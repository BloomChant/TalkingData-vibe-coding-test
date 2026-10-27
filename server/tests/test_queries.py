"""Query projections and evidence-based reconciliation across persisted state."""
import copy

from server.tests.test_ingestion import batch, initialized, obj, tree


def receipt(run='test', ack='AQ==', batches=1, objects=1, revision=1):
    return {
        'config':{'test_id':run,'workload':'objects','max_batches':8},
        'stats':{'acknowledged_batches':batches,'acknowledged_objects':objects},
        'bitmap_encoding':'lsb0','sent_bitmap':ack,'ack_bitmap':ack,
        'tree':{'acknowledged_revision':revision},
        'injections':{'fault_sent_bitmap':'AA==','fault_ack_bitmap':'AA==',
                      'fault_rejected_bitmap':'AA==','duplicate_ack_bitmap':'AA=='},
    }


def test_empty_database_has_readable_query_shapes(client):
    assert client.get('/api/runs').json()=={'runs':[]}
    assert client.get('/api/overview').json()['run'] is None
    assert client.get('/api/tree').json()['nodes']==[]
    assert client.get('/api/objects').json()=={'items':[],'next_cursor':None}
    assert client.get('/api/events').json()['events']==[]
    assert client.get('/api/quality').json()['rejected']==[]
    assert client.get('/api/frames').json()['frames']==[]


def test_subtree_counts_follow_taxonomy_move(client):
    initialized(client)
    assert client.post('/ingest',json=tree(revision=2,changes=[
        {'op':'create','node':{'type_id':3,'parent_id':2,'name':'观测组'}},
        {'op':'create','node':{'type_id':4,'parent_id':1,'name':'独立目录'}},
    ])).status_code==202
    assert client.post('/ingest',json=batch(objects=[obj('direct'),obj('nested',type_id=3)],revision=2)).status_code==202
    nodes={n['id']:n for n in client.get('/api/tree?run=test').json()['nodes']}
    assert nodes[2]['count']==1 and nodes[2]['subtree_count']==2
    items=client.get('/api/objects?run=test&type_id=2').json()['items']
    assert {i['id'] for i in items}=={'direct','nested'}
    assert client.post('/ingest',json=tree(revision=3,changes=[
        {'op':'move','node':{'type_id':3,'parent_id':4,'name':'观测组'}},
    ])).status_code==202
    nodes={n['id']:n for n in client.get('/api/tree?run=test').json()['nodes']}
    assert nodes[2]['subtree_count']==1 and nodes[4]['subtree_count']==1
    assert {i['id'] for i in client.get('/api/objects?run=test&type_id=2').json()['items']}=={'direct'}
    assert client.get('/api/objects/nested?run=test').json()['tree_revision']==2


def test_cursor_pagination_stays_disjoint_when_new_objects_arrive(client):
    initialized(client)
    assert client.post('/ingest',json=batch(objects=[obj(str(i)) for i in range(5)])).status_code==202
    first=client.get('/api/objects?run=test&limit=2').json()
    assert client.post('/ingest',json=batch(batch_id=1,objects=[obj('new')])).status_code==202
    second=client.get('/api/objects',params={'run':'test','limit':2,'cursor':first['next_cursor']}).json()
    third=client.get('/api/objects',params={'run':'test','limit':2,'cursor':second['next_cursor']}).json()
    pages=first['items']+second['items']+third['items']
    assert len(pages)==5 and len({i['id'] for i in pages})==5
    assert 'new' not in {i['id'] for i in pages}
    assert third['next_cursor'] is None


def test_chinese_search_is_literal_and_run_scoped(client):
    for run in ['test','other']:
        initialized(client,run)
        assert client.post('/ingest',json=batch(run,objects=[obj('a',name='恒星 A'),obj('b',name='卫星 B')])).status_code==202
    matching=client.get('/api/objects',params={'run':'test','q':'恒星'}).json()['items']
    assert [i['id'] for i in matching]==['a']
    assert client.get('/api/objects',params={'run':'test','q':"' OR 1=1 --"}).json()['items']==[]
    assert client.get('/api/objects?run=test&limit=101').status_code==422
    assert client.get('/api/objects?run=test&cursor=-1').status_code==422


def test_spatial_query_never_mixes_frame_or_unit(client):
    initialized(client)
    records=[]
    for i,(frame,unit) in enumerate([('g1','ly'),('g1','ly'),('g2','ly'),('g1','km')]):
        records.append(obj(str(i),position={'frame_id':frame,'frame_kind':'galactic','unit':unit,'x':i,'y':0,'z':1}))
    records.append(obj('unplaced'))
    assert client.post('/ingest',json=batch(objects=records)).status_code==202
    frames=client.get('/api/frames?run=test').json()['frames']
    assert {(f['frame_id'],f['unit'],f['count']) for f in frames}=={('g1','ly',2),('g2','ly',1),('g1','km',1)}
    response=client.get('/api/positions',params={'run':'test','frame_id':'g1','unit':'ly'}).json()
    assert response['sampled'] is True
    assert {p['id'] for p in response['points']}=={'0','1'}
    assert client.get('/api/overview?run=test').json()['positioned']==4


def test_reconciliation_pass_does_not_claim_unknown_checks_complete(client):
    initialized(client)
    assert client.post('/ingest',json=batch()).status_code==202
    response=client.post('/api/reconcile',json=receipt())
    assert response.status_code==200
    result=response.json()
    assert result['ok'] is True and result['complete'] is False
    assert result['missing_batch_ids']==[]
    assert any(c['status']=='unknown' for c in result['checks'])


def test_reconciliation_detects_missing_acknowledged_batch(client):
    initialized(client)
    assert client.post('/ingest',json=batch()).status_code==202
    result=client.post('/api/reconcile',json=receipt(ack='Aw==',batches=2,objects=2)).json()
    assert result['ok'] is False
    assert result['missing_batch_ids']==[1]
    assert any(c['status']=='fail' for c in result['checks'])


def test_reconciliation_rejects_invalid_and_out_of_bounds_bitmaps(client):
    initialized(client)
    assert client.post('/ingest',json=batch()).status_code==202
    bad=receipt();bad['ack_bitmap']='!'
    assert client.post('/api/reconcile',json=bad).status_code==422
    bad=receipt();bad['ack_bitmap']='Aw=='  # Acknowledges an unsent batch.
    assert client.post('/api/reconcile',json=bad).status_code==422
    bad=receipt();bad['config']['max_batches']=1;bad['ack_bitmap']=bad['sent_bitmap']='Ag=='
    assert client.post('/api/reconcile',json=bad).status_code==422
    bad=receipt();bad['config']['max_batches']=1_000_001
    assert client.post('/api/reconcile',json=bad).status_code==422


def test_reconciliation_separates_committed_but_unconfirmed_batch(client):
    initialized(client)
    assert client.post('/ingest',json=batch()).status_code==202
    assert client.post('/ingest',json=batch(batch_id=1,objects=[obj('second')])).status_code==202
    partial=receipt();partial['sent_bitmap']='Aw=='
    result=client.post('/api/reconcile',json=partial).json()
    assert result['missing_batch_ids']==[]
    assert result['unconfirmed_batch_ids']==[1]
    assert result['ok'] is True and result['complete'] is False


def test_unknown_run_returns_empty_query_views(client):
    assert client.get('/api/overview?run=not-arrived-yet').json()['run'] is None
    assert client.get('/api/tree?run=not-arrived-yet').json()['nodes']==[]
    assert client.get('/api/objects?run=not-arrived-yet').json()['items']==[]
    assert client.get('/api/events?run=not-arrived-yet').json()['events']==[]
    assert client.get('/api/frames?run=not-arrived-yet').json()['frames']==[]
    assert client.get('/api/quality?run=not-arrived-yet').json()['rejected']==[]


def test_committed_duplicate_still_acknowledged_at_storage_watermark(client, database, monkeypatch):
    initialized(client)
    assert client.post('/ingest',json=batch()).status_code==202
    monkeypatch.setattr(database,'STOP_BYTES',1)
    replay=client.post('/ingest',json=batch())
    assert replay.status_code==200 and replay.json()['duplicate'] is True
    fresh=client.post('/ingest',json=batch(batch_id=1,objects=[obj('new')]))
    assert fresh.status_code==507
    with database.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM objects').fetchone()[0]==1


def test_large_finite_coordinate_preserves_original_object(client):
    initialized(client)
    large=10**30
    record=obj(position={'frame_id':'g1','frame_kind':'galactic','unit':'ly','x':large,'y':0,'z':1})
    result=client.post('/ingest',json=batch(objects=[record]))
    assert result.status_code==202
    detail=client.get('/api/objects/object-1?run=test').json()['object']
    assert detail['position']['x']==large and type(detail['position']['x']) is int
    points=client.get('/api/positions',params={'run':'test','frame_id':'g1','unit':'ly'}).json()['points']
    assert points[0]['x']==float(large)
