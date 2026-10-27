"""Semantic checks for the additive frontend contract v1.1."""
from server.tests.test_ingestion import batch, initialized, obj, tree


def populated(client):
    initialized(client)
    assert client.post('/ingest',json=tree(revision=2,changes=[
        {'op':'create','node':{'type_id':3,'parent_id':1,'name':'生命'}},
    ])).status_code==202
    def position(frame,kind='galactic'):
        return {'frame_id':frame,'frame_kind':kind,'unit':'ly','x':1,'y':2,'z':3}
    records=[
        obj('a',name='探测器 #1',position=position('galaxy-1/system-1'),attributes={'mass':1,'tags':['a','b'],'nullable':None}),
        obj('b',name='探测器 #2',type_id=3,position=None,attributes={'mass':'unknown','tags':['c'],'nested':{'x':True}}),
        obj('c',name='恒星 #3',position=position('galaxy-10/system-1'),attributes={'mass':3}),
        obj('d',name='恒星 #4',type_id=3,position=position('galaxy-1','local_surface'),attributes={}),
    ]
    assert client.post('/ingest',json=batch(objects=records,revision=2)).status_code==202


def test_contract_filter_or_within_dimension_and_between_dimensions(client):
    populated(client)
    response=client.get('/api/objects',params=[('run','test'),('type_id','2'),('type_id','3'),('kind','探测器'),('frame_kind','none')])
    assert response.status_code==200
    value=response.json()
    assert [i['id'] for i in value['items']]==['b']
    assert value['total']==1 and value['total_exact'] is True
    value=client.get('/api/objects',params=[('run','test'),('kind','探测器'),('kind','恒星')]).json()
    assert value['total']==4


def test_contract_frame_prefix_is_path_segment_not_text_prefix(client):
    populated(client)
    value=client.get('/api/objects',params={'run':'test','frame':'galaxy-1'}).json()
    assert {i['id'] for i in value['items']}=={'a','d'}
    assert value['total']==2


def test_contract_total_is_filter_count_not_page_count(client):
    populated(client)
    value=client.get('/api/objects?run=test&limit=1').json()
    assert len(value['items'])==1 and value['total']==4 and value['total_exact'] is True
    second=client.get('/api/objects',params={'run':'test','limit':1,'cursor':value['next_cursor']}).json()
    assert second['total']==4 and second['items'][0]['id']!=value['items'][0]['id']


def test_contract_facets_count_direct_types_and_null_positions(client):
    populated(client)
    response=client.get('/api/facets?run=test&dims=type,kind,frame_kind,galaxy')
    assert response.status_code==200
    value=response.json()
    assert value['total']==4 and value['filters_applied'] is True
    bucket=lambda dim:{b['value']:b['count'] for b in value['facets'][dim]['buckets']}
    assert bucket('type')=={'2':2,'3':2}
    assert bucket('kind')=={'探测器':2,'恒星':2}
    assert bucket('frame_kind')['none']==1
    assert bucket('galaxy')=={'galaxy-1':2,'galaxy-10':1,'none':1}


def test_contract_fields_preserve_mixed_types_and_document_coverage(client):
    populated(client)
    response=client.get('/api/fields?run=test&sample=2000')
    assert response.status_code==200
    value=response.json()
    assert value['filters_applied'] is True
    assert value['sample']=={'size':4,'matched':4,'sampled':False,'order':'most_recent'}
    fields={f['path']:f for f in value['fields']}
    mass=fields['attributes.mass']
    assert mass['present']==3 and mass['coverage']==.75
    assert mass['types']=={'number':2,'string':1} and mass['mixed'] is True
    assert mass['numeric']=={'min':1,'max':3}
    assert fields['attributes.tags[]']['present']==2  # Per object, not array element count.
    assert fields['attributes.nullable']['types']=={'null':1}
    assert fields['attributes.nullable']['mixed'] is False


def test_contract_fields_filter_before_recent_sampling(client):
    populated(client)
    value=client.get('/api/fields',params={'run':'test','kind':'探测器','sample':1}).json()
    assert value['sample']=={'size':1,'matched':2,'sampled':True,'order':'most_recent'}
    fields={f['path']:f for f in value['fields']}
    assert fields['attributes.mass']['types']=={'string':1}


def test_contract_empty_additions_are_readable(client):
    for path,key in [('/api/facets','facets'),('/api/fields','fields')]:
        response=client.get(path+'?run=not-arrived-yet')
        assert response.status_code==200 and key in response.json()


def test_contract_overview_gap_buckets_keep_full_shape(client):
    initialized(client)
    assert client.post('/ingest',json=batch()).status_code==202
    series=client.get('/api/overview?run=test').json()['series']
    assert len(series)==60
    expected={'run','second','objects','batches','duplicates','rejected','latency_sum','latency_count','latency_max'}
    assert all(expected<=set(row) and row['run']=='test' for row in series)
    empty=[row for row in series if row['latency_count']==0]
    assert empty and all(row['latency_sum']==row['latency_max']==0 for row in empty)


def test_contract_reconcile_returns_unavailable_before_first_check(client):
    initialized(client)
    response=client.get('/api/reconcile?run=test')
    assert response.status_code==200
    assert response.json()['run']=='test' and response.json()['available'] is False


def test_contract_reconcile_bitmap_and_result_survive_reinitialization(client,database):
    import time
    from fastapi.testclient import TestClient
    from server.app.main import app
    from server.tests.test_queries import receipt
    initialized(client)
    assert client.post('/ingest',json=batch()).status_code==202
    assert client.post('/ingest',json=batch(batch_id=2,objects=[obj('unconfirmed')])).status_code==202
    submitted=receipt(ack='Aw==',batches=2,objects=2)
    submitted['sent_bitmap']='Dw=='
    before=time.time()
    response=client.post('/api/reconcile',json=submitted)
    assert response.status_code==200
    result=response.json()
    assert result['batches']=={'size':8,'states':'AMUX----'}
    assert before-1<=result['checked_at']<=time.time()+1
    assert result['ok'] is False and result['missing_batch_ids']==[1]
    database.init()
    with TestClient(app) as restarted:
        loaded=restarted.get('/api/reconcile?run=test')
        assert loaded.status_code==200 and loaded.json()==result
        initialized(restarted,'other')
        assert restarted.get('/api/reconcile?run=other').json()['available'] is False


def test_contract_facets_ignore_only_their_own_filter(client):
    populated(client)
    value=client.get('/api/facets',params={'run':'test','type_id':2,'kind':'探测器','dims':'type,kind,frame_kind'}).json()
    assert value['total']==1
    bucket=lambda dim:{b['value']:b['count'] for b in value['facets'][dim]['buckets']}
    assert bucket('type')=={'2':1,'3':1}  # Ignore type_id; keep kind.
    assert bucket('kind')=={'探测器':1,'恒星':1}  # Ignore kind; keep type_id.
    assert bucket('frame_kind')=={'galactic':1}  # Both other filters remain.
    value=client.get('/api/facets',params={'run':'test','kind':'探测器','frame_kind':'none','dims':'frame_kind'}).json()
    assert value['total']==1
    assert {b['value']:b['count'] for b in value['facets']['frame_kind']['buckets']}=={'galactic':1,'none':1}


def test_contract_galaxy_facet_ignores_frame_but_keeps_other_filters(client):
    populated(client)
    value=client.get('/api/facets',params={'run':'test','frame':'galaxy-1','kind':'恒星','dims':'galaxy'}).json()
    assert value['total']==1
    assert {b['value']:b['count'] for b in value['facets']['galaxy']['buckets']}=={'galaxy-1':1,'galaxy-10':1}


def test_contract_kind_derivation_uses_numeric_suffix_and_literal_name(client):
    initialized(client)
    records=[
        obj('plain',name='foo'),obj('numeric',name='foo #12'),obj('joined',name='foo#12'),
        obj('nonnumeric',name='foo #abc'),obj('suffix-tail',name='foo #12x'),
        obj('percent',name='a%b #1'),obj('percent-lookalike',name='axb #1'),
        obj('underscore',name='a_b #2'),obj('underscore-lookalike',name='acb #2'),
    ]
    assert client.post('/ingest',json=batch(objects=records)).status_code==202
    def identifiers(kind):
        response=client.get('/api/objects',params={'run':'test','kind':kind})
        assert response.status_code==200
        return {item['id'] for item in response.json()['items']}
    assert identifiers('foo')=={'plain','numeric','joined'}
    assert identifiers('foo #abc')=={'nonnumeric'}
    assert identifiers('a%b')=={'percent'}
    assert identifiers('a_b')=={'underscore'}


def test_contract_frame_prefix_treats_sql_wildcards_literally(client):
    initialized(client)
    def placed(identifier,frame):
        return obj(identifier,position={'frame_id':frame,'frame_kind':'galactic','unit':'ly','x':1,'y':2,'z':3})
    records=[placed('exact','g%_1'),placed('child','g%_1/system'),
             placed('text-prefix','g%_10/system'),placed('lookalike','galaxy1/system'),
             placed('percent-lookalike','gXYZ_1/system'),placed('underscore-lookalike','g%X1/system')]
    assert client.post('/ingest',json=batch(objects=records)).status_code==202
    response=client.get('/api/objects',params={'run':'test','frame':'g%_1'})
    assert response.status_code==200
    value=response.json()
    assert {item['id'] for item in value['items']}=={'exact','child'}
    assert value['total']==2
