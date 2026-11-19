import json
import pytest
from fastapi.testclient import TestClient

from corpus.app import create_app
from corpus.config import Settings
from helpers import paper_body


def save(client,papers,projects=None,**extra):
    return client.post('/api/capture',json={'papers':papers,'projects':projects or ['sbi-pretrain','diff-compose'],**extra})


def test_capture_one_record_in_several_projects_and_export(app_client):
    client,_=app_client
    response=save(client,[paper_body('multi')])
    assert response.status_code==200,response.text
    paper=response.json()['results'][0]['paper']
    assert response.json()['added']==1 and paper['state']=='unreviewed'
    assert paper['rel'] is None and paper['admission']=='approved'
    assert set(paper['projects'])=={'sbi-pretrain','diff-compose'}
    for query in ('field:sbi-pretrain','project:diff-compose','project:sbi-pretrain AND project:diff-compose'):
        result=client.get('/api/papers',params={'q':query}).json()
        assert result['total']==1 and result['papers'][0]['id']==paper['id']
    assert client.get('/api/papers',params={'facets':json.dumps({'field':['diff-compose']})}).json()['total']==1
    stats=client.get('/api/stats').json()
    assert stats['total']==1
    assert {r['field']:r['count'] for r in stats['fields']}=={'sbi-pretrain':1,'diff-compose':1,'unfiled':0}
    exported=client.post('/api/export',json={'format':'json','scope':'all'}).json()
    assert set(json.loads(exported['text'])[0]['projects'])==set(paper['projects'])
    bib=client.post('/api/export',json={'format':'bibtex','scope':'query','q':'project:diff-compose'}).json()
    assert bib['count']==1 and 'topic=diff-compose' in bib['text']


def test_existing_capture_is_additive_and_preserves_notes_ratings_and_pdfs(app_client):
    client,_=app_client
    original=client.post('/api/papers',json=paper_body('keep',notes='Keep my annotation',state='reviewed',rel=9,tags=['mine'],artifacts=[{'kind':'pdf','url':'https://example.org/original.pdf'}])).json()['paper']
    incoming=paper_body('keep',notes='Overwrite',rel=1,title='A revised publisher title',artifacts=[])
    result=save(client,[incoming],projects=['diff-compose']).json()['results'][0]
    assert result['status']=='filed'
    updated=result['paper']
    for name in ('id','title','field','notes','rel','state','tags','artifacts'):
        assert updated[name]==original[name],name
    assert set(updated['projects'])=={'sbi-pretrain','diff-compose'}
    assert save(client,[incoming],projects=['diff-compose']).json()['results'][0]['status']=='exists'
    assert client.get('/api/papers').json()['library_total']==1


@pytest.mark.parametrize('status',['pending','dismissed'])
def test_existing_inbox_paper_needs_explicit_manual_admission(app_client,status):
    client,settings=app_client
    proposal=paper_body('queued')
    imported=client.post('/api/ingest',headers={'Authorization':'Bearer '+settings.ingest_token},json={'papers':[proposal]}).json()
    pid=imported['results'][0]['id']
    if status=='dismissed': assert client.post('/api/inbox/decision',json={'id':pid,'action':'dismiss'}).status_code==200
    response=save(client,[proposal]).json()['results'][0]
    assert response['status']=='needs_review'
    assert client.get('/api/papers').json()['total']==0
    response=save(client,[{**proposal,'admit_existing':True}]).json()['results'][0]
    assert response['paper']['id']==pid and response['paper']['admission']=='approved'
    assert response['paper']['state']=='unreviewed' and response['paper']['last_decision']['after_admission']=='approved'


def test_capture_is_session_only_and_protected_from_cross_origin_writes(tmp_path):
    settings=Settings(data_dir=tmp_path,password='owner-password',pdf_jobs=False,ingest_token='agent-token')
    with TestClient(create_app(settings)) as client:
        body={'projects':['unfiled'],'papers':[{'title':'Manual paper'}]}
        assert client.post('/api/capture',json=body).status_code==401
        assert client.post('/api/capture',headers={'Authorization':'Bearer agent-token'},json=body).status_code==401
        assert client.get('/api/catalog/search',params={'q':'physics'}).status_code==401
        client.post('/api/login',json={'password':'owner-password'})
        assert client.post('/api/capture',headers={'Origin':'https://attacker.example'},json=body).status_code==403
        assert client.post('/api/capture',json=body).status_code==200


def test_ingest_retains_approval_gate_with_multiple_projects(app_client):
    client,settings=app_client
    response=client.post('/api/ingest',headers={'Authorization':'Bearer '+settings.ingest_token},json={'papers':[paper_body('multi-inbox',projects=['sbi-pretrain','diff-compose'])]})
    assert response.json()['queued']==1
    assert client.get('/api/inbox',params={'field':'diff-compose'}).json()['total']==1
    assert client.get('/api/papers').json()['total']==0
