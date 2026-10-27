"""Experiment ownership and recovery using a fake gateway, never a live generator."""
import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.tests.test_ingestion import batch, initialized
from server.tests.test_queries import receipt


class FakeGenerator:
    def __init__(self):
        self.status={'state':'idle'}
        self.calls=[]
        self.receipt=None
        self.fail_start=False
        self.offline=False

    def __call__(self,path,payload=None):
        self.calls.append((path,copy.deepcopy(payload)))
        if self.offline:
            raise OSError('simulated connection refused')
        if path=='/status': return copy.deepcopy(self.status)
        if path=='/start':
            self.status={'state':'running','config':copy.deepcopy(payload)}
            if self.fail_start: raise TimeoutError('simulated lost start response')
            return copy.deepcopy(self.status)
        if path=='/stop':
            self.status['state']='finished'
            return copy.deepcopy(self.status)
        if path=='/receipt': return copy.deepcopy(self.receipt)
        raise AssertionError('Unexpected gateway path '+path)


@pytest.fixture
def controls(database,monkeypatch):
    from server.app import experiments
    from server.app.main import ingest
    from server.app.queries import router as queries
    fake=FakeGenerator()
    monkeypatch.setattr(experiments,'gateway',fake)
    app=FastAPI()  # No background monitor: poll deterministically without real network access.
    app.include_router(experiments.router,prefix='/api')
    app.include_router(queries,prefix='/api')
    app.add_api_route('/ingest',ingest,methods=['POST'])
    with TestClient(app) as client:
        yield client,experiments,fake


@pytest.mark.parametrize('params',[{'rate':0},{'duration':601},{'workers':9},{'batch_size':501},{'rate':True},{'scenario':'unknown'}, {'target_url':'http://attacker.invalid/ingest'}, {'generator_url':'http://attacker.invalid'}])
def test_experiment_rejects_invalid_or_injected_parameters(controls,params):
    client,_,fake=controls
    assert client.post('/api/experiment/start',json=params).status_code==422
    assert fake.calls==[]


def test_experiment_start_uses_only_configured_target(controls):
    client,module,fake=controls
    response=client.post('/api/experiment/start',json={})
    assert response.status_code==200
    result=response.json()
    sent=[payload for path,payload in fake.calls if path=='/start']
    assert len(sent)==1 and sent[0]['target_url']==module.GENERATOR_TARGET_URL
    assert sent[0]['objects_per_second']==200 and sent[0]['duration_seconds']==60
    assert sent[0]['test_id']==result['test_id']
    assert module.latest()['state']=='running'


def test_experiment_concurrent_start_sends_only_one_start(controls):
    client,_,fake=controls
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes=list(pool.map(lambda _:client.post('/api/experiment/start',json={}).status_code,range(2)))
    assert sorted(codes)==[200,409]
    assert sum(path=='/start' for path,_ in fake.calls)==1


def test_experiment_external_occupancy_is_not_interrupted(controls):
    client,_,fake=controls
    fake.status={'state':'running','config':{'test_id':'external-run'}}
    assert client.post('/api/experiment/start',json={}).status_code==409
    assert client.post('/api/experiment/stop',json={'test_id':'external-run'}).status_code==409
    assert all(path not in ('/start','/stop') for path,_ in fake.calls)


def test_experiment_cannot_stop_replacement_external_run(controls):
    client,_,fake=controls
    owned=client.post('/api/experiment/start',json={}).json()['test_id']
    fake.status={'state':'running','config':{'test_id':'external-replacement'}}
    assert client.post('/api/experiment/stop',json={'test_id':owned}).status_code==409
    assert all(path!='/stop' for path,_ in fake.calls)


def test_experiment_stop_requires_matching_owned_identity(controls):
    client,module,fake=controls
    owned=client.post('/api/experiment/start',json={}).json()['test_id']
    assert client.post('/api/experiment/stop',json={'test_id':'wrong'}).status_code==409
    response=client.post('/api/experiment/stop',json={'test_id':owned})
    assert response.status_code==200 and module.latest()['state']=='stopping'
    assert sum(path=='/stop' for path,_ in fake.calls)==1


def test_experiment_unknown_start_outcome_is_observed_without_resend(controls):
    client,module,fake=controls
    fake.fail_start=True
    response=client.post('/api/experiment/start',json={})
    assert response.status_code==202
    assert response.json()['state']=='starting' and response.json()['error']
    assert client.post('/api/experiment/start',json={}).status_code==409
    module.poll_once()
    assert module.latest()['state']=='running'
    assert sum(path=='/start' for path,_ in fake.calls)==1


def test_experiment_transport_failure_has_readable_status(controls):
    client,module,fake=controls
    fake.offline=True
    response=client.get('/api/experiment')
    assert response.status_code==200
    assert response.json()['reachable'] is False and 'connection refused' in response.json()['error']
    response=client.post('/api/experiment/start',json={})
    assert response.status_code==503 and 'Generator unavailable' in response.json()['detail']
    assert module.latest() is None


def test_experiment_completion_reconciles_and_persists(controls,database):
    client,module,fake=controls
    owned=client.post('/api/experiment/start',json={}).json()['test_id']
    initialized(client,owned)
    assert client.post('/ingest',json=batch(owned)).status_code==202
    fake.receipt=receipt(run=owned)
    fake.status={'state':'finished','config':{'test_id':owned},'stats':{'state':'completed','finish_reason':'duration'}}
    module.poll_once()
    saved=module.latest()
    assert saved['state']=='completed' and saved['finished_at']>=saved['created_at']
    assert saved['receipt']==fake.receipt and saved['reconciliation']['ok'] is True
    assert saved['reconciliation']['complete'] is False
    database.init()
    assert module.latest()==saved
    assert client.get('/api/reconcile',params={'run':owned}).json()['available'] is True
    module.poll_once()
    assert sum(path=='/receipt' for path,_ in fake.calls)==1


def test_experiment_receipt_mismatch_is_never_reconciled(controls):
    client,module,fake=controls
    owned=client.post('/api/experiment/start',json={}).json()['test_id']
    fake.status={'state':'finished','config':{'test_id':owned}}
    fake.receipt=receipt(run='foreign')
    module.poll_once()
    saved=module.latest()
    assert saved['reconciliation'] is None and 'identity mismatch' in saved['error']
    assert client.get('/api/reconcile?run=foreign').json()['available'] is False


def test_experiment_failed_generator_is_not_reported_as_success(controls):
    client,module,fake=controls
    owned=client.post('/api/experiment/start',json={}).json()['test_id']
    initialized(client,owned)
    assert client.post('/ingest',json=batch(owned)).status_code==202
    fake.receipt=receipt(run=owned)
    fake.receipt['stats'].update(state='failed',finish_reason='tree_error')
    fake.status={'state':'finished','config':{'test_id':owned},'error':'tree delivery failed',
                 'stats':{'state':'failed','finish_reason':'tree_error'}}
    module.poll_once()
    saved=module.latest()
    assert saved['state']=='failed' and saved['error']
    assert saved['receipt']==fake.receipt and saved['reconciliation']['ok'] is True
