import copy
import json
import zlib
from concurrent.futures import ThreadPoolExecutor

import pytest


def tree(run='test', revision=1, changes=None):
    return {'kind':'types', 'test_id':run, 'revision':revision,
            'changes': changes if changes is not None else [
                {'op':'create','node':{'type_id':1,'parent_id':0,'name':'万物'}},
                {'op':'create','node':{'type_id':2,'parent_id':1,'name':'星体'}}]}


def obj(identifier='object-1', **updates):
    value = {'id':identifier, 'sequence':1, 'type_id':2, 'name':'恒星 🌟',
             'timestamp_ms':1792627200000, 'position':None,
             'attributes':{'mass_solar':2.36, 'tags':['中文','sample'], 'nullable':None}}
    value.update(updates)
    return value


def batch(run='test', batch_id=0, objects=None, revision=1):
    return {'kind':'objects', 'test_id':run, 'batch_id':batch_id,
            'tree_revision':revision, 'objects':objects if objects is not None else [obj()]}


def initialized(client, run='test'):
    result=client.post('/ingest',json=tree(run))
    assert result.status_code==202, result.text


def counts(database, run='test'):
    with database.connect() as db:
        return dict(db.execute('SELECT * FROM runs WHERE id=?',(run,)).fetchone())


def test_commit_preserves_original_payload_and_duplicate_is_idempotent(client, database):
    initialized(client)
    body=json.dumps(batch(),ensure_ascii=False,indent=3).encode()
    first=client.post('/ingest',content=body,headers={'content-type':'application/json'})
    assert first.status_code==202
    assert first.json()['inserted_objects']==1
    second=client.post('/ingest',json=batch())  # Different whitespace, equivalent JSON.
    assert second.status_code==200
    assert second.json()['duplicate'] is True
    with database.connect() as db:
        raw=db.execute("SELECT body_z FROM messages WHERE kind='objects'").fetchone()[0]
        assert zlib.decompress(raw)==body
        assert db.execute('SELECT COUNT(*) FROM objects').fetchone()[0]==1
    row=counts(database)
    assert (row['objects'],row['batches'],row['duplicates'])==(1,1,1)


def test_conflicting_batch_does_not_overwrite_committed_data(client, database):
    initialized(client)
    assert client.post('/ingest',json=batch()).status_code==202
    different=batch(objects=[obj(name='changed')])
    assert client.post('/ingest',json=different).status_code==409
    with database.connect() as db:
        assert db.execute('SELECT name FROM objects').fetchone()[0]=='恒星 🌟'
        assert db.execute('SELECT COUNT(*) FROM rejected').fetchone()[0]==1
    assert counts(database)['objects']==1


@pytest.mark.parametrize('mutation', ['missing_type','array_attributes','missing_position','bool_sequence'])
def test_invalid_object_rejects_entire_batch(client, database, mutation):
    initialized(client)
    bad=obj('bad')
    if mutation=='missing_type': del bad['type_id']
    elif mutation=='array_attributes': bad['attributes']=[]
    elif mutation=='missing_position': del bad['position']
    else: bad['sequence']=True
    result=client.post('/ingest',json=batch(objects=[obj('valid-first'),bad]))
    assert result.status_code==422
    with database.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM objects').fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM messages WHERE kind='objects'").fetchone()[0]==0
    assert counts(database)['objects']==0


def test_database_level_rejection_rolls_back_earlier_object(client, database):
    initialized(client)
    response=client.post('/ingest',json=batch(objects=[obj('first'),obj('unknown',type_id=999)]))
    assert response.status_code==422
    assert counts(database)['objects']==0
    with database.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM objects').fetchone()[0]==0


def test_unknown_attributes_null_and_mixed_types_are_allowed(client, database):
    initialized(client)
    records=[obj('a',attributes={'new':{'nested':[1,'1',None,False,{'中':'文'}]}}),
             obj('b',attributes={'new':42})]
    assert client.post('/ingest',json=batch(objects=records)).status_code==202
    assert counts(database)['objects']==2


def test_taxonomy_rename_move_and_cycle_rollback(client, database):
    initialized(client)
    changes=[{'op':'create','node':{'type_id':3,'parent_id':1,'name':'待分类'}},
             {'op':'rename','node':{'type_id':2,'parent_id':1,'name':'天体'}},
             {'op':'move','node':{'type_id':2,'parent_id':3,'name':'天体'}}]
    assert client.post('/ingest',json=tree(revision=2,changes=changes)).status_code==202
    assert client.post('/ingest',json=tree(revision=2,changes=changes)).status_code==200
    cyclic=tree(revision=3,changes=[{'op':'move','node':{'type_id':3,'parent_id':2,'name':'待分类'}}])
    assert client.post('/ingest',json=cyclic).status_code==422
    with database.connect() as db:
        node=db.execute('SELECT parent,name FROM types WHERE id=2').fetchone()
        assert tuple(node)==(3,'天体')
    assert counts(database)['revision']==2


def test_revision_gap_is_retryable_without_acknowledgement(client, database):
    initialized(client)
    result=client.post('/ingest',json=tree(revision=3,changes=[]))
    assert result.status_code==503
    assert result.headers.get('retry-after')=='1'
    assert counts(database)['revision']==1
    result=client.post('/ingest',json=batch(revision=2))
    assert result.status_code==503
    assert counts(database)['objects']==0


def test_run_isolation_and_conflicting_object_id(client, database):
    for run in ['one','two']:
        initialized(client,run)
        assert client.post('/ingest',json=batch(run)).status_code==202
    assert client.post('/ingest',json=batch('one',1,[obj(name='changed')])).status_code==409
    assert counts(database,'one')['objects']==1
    assert counts(database,'two')['objects']==1


def test_simultaneous_duplicates_apply_once(client, database):
    initialized(client)
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses=list(pool.map(lambda _:client.post('/ingest',json=batch()),range(12)))
    assert all(r.status_code in (200,202) for r in responses)
    assert sum(r.status_code==202 for r in responses)==1
    assert counts(database)['objects']==1
    assert counts(database)['duplicates']==11


def test_storage_watermark_rejects_without_insertion(client, database, monkeypatch):
    initialized(client)
    monkeypatch.setattr(database,'STOP_BYTES',1)
    response=client.post('/ingest',json=batch())
    assert response.status_code==507
    assert counts(database)['objects']==0


def test_request_size_limit(client):
    response=client.post('/ingest',content=b' '*(8*1024**2+1))
    assert response.status_code==413


@pytest.mark.parametrize('body',[b'{',b'{"kind":"objects","value":NaN}',b'{"kind":"objects","value":Infinity}'])
def test_invalid_json_is_rejected_and_preserved(client, database, body):
    assert client.post('/ingest',content=body).status_code==400
    with database.connect() as db:
        row=db.execute('SELECT body_z FROM rejected').fetchone()
        assert row is not None and zlib.decompress(row[0])==body


def test_restart_retains_committed_data(database):
    from fastapi.testclient import TestClient
    from server.app.main import app
    with TestClient(app) as c:
        initialized(c)
        assert c.post('/ingest',json=batch()).status_code==202
    with TestClient(app) as c:
        assert c.get('/health').status_code==200
        assert c.post('/ingest',json=batch()).status_code==200
    assert counts(database)['objects']==1


def test_oversized_coordinate_is_validation_error(client):
    initialized(client)
    huge=obj(position={'frame_id':'galaxy-1','frame_kind':'galactic','unit':'ly','x':10**400,'y':0,'z':0})
    result=client.post('/ingest',json=batch(objects=[huge]))
    assert result.status_code in (400,422)


def test_unpaired_unicode_surrogate_is_validation_error(client):
    initialized(client)
    message=batch(objects=[obj(name='\ud800')])
    result=client.post('/ingest',content=json.dumps(message).encode())
    assert result.status_code in (400,422)
