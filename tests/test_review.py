"""Evidence boundaries, review preparation, and decisions across the real API."""
import json

import pytest
from fastapi.testclient import TestClient

from corpus.app import create_app
from corpus.config import Settings
from corpus.db import connection
from corpus import jobs, sources
from helpers import INBOX_TOKEN as TOKEN, assessment, decision, ingest, paper, pdf_bytes, proposal


def prepare_pdf(client, settings, monkeypatch, pid):
    monkeypatch.setattr(sources,'safe_download',lambda *args,**kwargs:pdf_bytes())
    queued = client.post('/api/review/check',json={'id':pid})
    assert queued.status_code==200, queued.text
    artifact = jobs.next_artifact(settings)
    assert artifact and artifact['paper_id']==pid
    jobs.fetch_one(settings,artifact)
    return paper(client,pid)['review']


def complete(client, pid, review, **changes):
    return client.post('/api/review/assessments',json={
        'id':pid, **assessment(stage='full_text',priority='medium',confidence='high',
         explanation='The inspected experiment supplies equation information.',questions=[]),
        'abstract_id':review['abstract']['id'], 'source_version':review['abstract']['source_version'],
        'expected_assessment_id':review['current']['id'],
        'sections':['Results'], 'evidence':[{'page':1,'quote':'posterior collapse'}], **changes})


@pytest.mark.parametrize('priority,questions,queued',[
    ('high',[],True), ('medium',['Does it forecast dynamics?'],True), ('medium',[],False), ('low',[],False), ('exclude',[],False),
])
def test_preparation_policy_and_independent_confidence(library,priority,questions,queued):
    client, settings=library
    pid=ingest(client,proposal(assessment=assessment(priority=priority,questions=questions,confidence='low')))['results'][0]['id']
    review=paper(client,pid)['review']
    assert review['current']['priority']==priority and review['current']['confidence']=='low'
    assert bool(review['check'])==queued
    assert (jobs.next_artifact(settings) is not None)==queued
    assert client.get('/api/papers').json()['total']==0
    assert client.post('/api/export',json={'scope':'all','format':'json'}).json()['count']==0


def test_completed_check_keeps_abstract_and_records_supporting_pages(library,monkeypatch):
    client,settings=library
    pid=ingest(client,proposal(assessment=assessment()))['results'][0]['id']
    before=paper(client,pid)['review']['current']
    review=prepare_pdf(client,settings,monkeypatch,pid)
    assert review['check']['status']=='ready'
    assert review['current']['stage']=='abstract' # A cached PDF is not a scientific check.
    assert client.get('/api/papers',params={'q':'pdf:"posterior collapse"'}).json()['total']==0
    response=complete(client,pid,review)
    assert response.status_code==200,response.text
    after=paper(client,pid)['review']
    assert after['check']['status']=='checked' and after['current']['priority']=='medium'
    assert after['abstract']==before
    assert after['current']['compared_to']==before['id']
    assert after['current']['evidence']==[{'page':1,'quote':'posterior collapse'}]
    assert client.get('/api/inbox',params={'evidence':'full_text'}).json()['total']==1
    assert client.get('/api/review/text',params={'id':pid,'page':1}).json()['pages']==1
    assert client.get('/api/review/text',params={'id':pid,'page':2}).status_code==404


@pytest.mark.parametrize('changes',[
    {'evidence':[]}, {'sections':[]}, {'evidence':[{'page':2,'quote':'posterior collapse'}]},
    {'evidence':[{'page':1,'quote':'A passage that does not exist'}]},
    {'evidence':[{'page':True,'quote':'posterior collapse'}]}, {'source_version':'v999'},
])
def test_full_text_claim_requires_valid_source_evidence(library,monkeypatch,changes):
    client,settings=library
    pid=ingest(client,proposal(assessment=assessment()))['results'][0]['id']
    review=prepare_pdf(client,settings,monkeypatch,pid)
    assert complete(client,pid,review,**changes).status_code==400
    assert paper(client,pid)['review']['current']['stage']=='abstract'


def test_unavailable_text_preserves_abstract_and_never_blocks_approval(library,monkeypatch):
    client,settings=library
    pid=ingest(client,proposal(assessment=assessment()))['results'][0]['id']
    def failed(*args,**kwargs): raise ValueError('Source unavailable')
    monkeypatch.setattr(sources,'safe_download',failed)
    jobs.fetch_one(settings,jobs.next_artifact(settings))
    review=paper(client,pid)['review']
    assert review['check']['status']=='unavailable'
    assert review['current']['stage']=='abstract'
    assert decision(client,pid,'approve',assessment_id=review['current']['id']).status_code==200
    assert paper(client,pid)['last_decision']['assessment_id']==review['current']['id']


def test_agent_can_supply_evidence_but_cannot_decide(tmp_path):
    settings=Settings(data_dir=tmp_path,password='owner-secret',pdf_jobs=False,ingest_token=TOKEN)
    with TestClient(create_app(settings)) as client:
        response=client.post('/api/ingest',headers={'Authorization':'Bearer '+TOKEN},json={'papers':[{'title':'Imported reference','abstract':'Physical modeling.'}]})
        pid=response.json()['results'][0]['id']
        assert client.get('/api/review/work').status_code==401
        headers={'Authorization':'Bearer '+TOKEN}
        work=client.get('/api/review/work',headers=headers)
        assert work.status_code==200 and work.json()['items'][0]['id']==pid
        expected=work.json()['items'][0]['review']['current']['id']
        assert client.post('/api/review/assessments',headers=headers,json={'id':pid,'expected_assessment_id':expected,**assessment(priority='medium')}).status_code==200
        assert client.post('/api/inbox/decision',headers=headers,json={'id':pid,'action':'approve'}).status_code==401
        assert client.get('/api/review/work',headers={'Authorization':'Bearer wrong'}).status_code==401


def test_criteria_change_preserves_but_invalidates_old_assessments(library):
    client,_=library
    pid=ingest(client,proposal(assessment=assessment(priority='medium')))['results'][0]['id']
    old=paper(client,pid)['review']['current']
    response=client.post('/api/topics',json={'id':'physics','label':'Physics','relevance_criteria':'Require dynamics evidence.'})
    assert response.status_code==200
    new=paper(client,pid)['review']
    assert new['current']['assessor']=='automatic-topic-match-v1'
    assert new['current']['id'] != old['id']
    assert new['current']['confidence'] == 'low'
    assert old['id'] in [entry['id'] for entry in new['history']]


def test_dismissing_cancels_pending_review_download(library,monkeypatch):
    client,settings=library
    pid=ingest(client,proposal(assessment=assessment()))['results'][0]['id']
    artifact=jobs.next_artifact(settings)
    assert decision(client,pid,'dismiss').status_code==200
    downloads=[]
    monkeypatch.setattr(sources,'safe_download',lambda *args,**kwargs:downloads.append(args))
    jobs.fetch_one(settings,artifact)
    assert downloads==[]


def test_automatic_topic_match_on_arrival_and_edit(library):
    client, settings = library
    with connection(settings) as db:
        db.execute("UPDATE fields SET relevance_criteria='physical regimes' WHERE id='physics'")
    pid = ingest(client, proposal(proposed_scores={}))['results'][0]['id']
    item = paper(client, pid)
    review = item['review']['current']
    assert review['assessor'] == 'automatic-topic-match-v1'
    assert review['confidence'] == 'low'
    assert 'physical, regimes' in review['explanation']
    assert item['proposed_rel'] == 10 and item['rel'] is None
    assert item['proposed_cred'] is None and item['proposed_qual'] is None
    assert item['review']['check'] is None
    assert client.get('/api/inbox', params={'evidence':'abstract'}).json()['total'] == 1
    with connection(settings) as db:
        from corpus.review import ensure_abstract
        assert ensure_abstract(db,pid)['id'] == review['id']
        assert db.execute('SELECT count(*) FROM review_assessments WHERE paper_id=?',(pid,)).fetchone()[0] == 1


def test_startup_backfills_existing_unassessed_inbox(library):
    client, settings = library
    pid = ingest(client, proposal(proposed_scores={}))['results'][0]['id']
    from corpus.review import prepare_pending
    with connection(settings) as db:
        # Simulate a pending record created before automatic matching existed.
        db.execute("UPDATE fields SET relevance_criteria='physical regimes' WHERE id='physics'")
        from corpus.review import subject
        _, snapshot, digest, _ = subject(db, pid)
        db.execute('UPDATE review_assessments SET input_hash=?,snapshot=? WHERE paper_id=?',
                   (digest, json.dumps(snapshot), pid))
    before = paper(client,pid)['review']['current']
    assert before['assessor'] == 'not-assessed'
    with connection(settings) as db:
        prepare_pending(db)
        prepare_pending(db)
    after = paper(client,pid)
    assert after['review']['current']['assessor'] == 'automatic-topic-match-v1'
    assert after['proposed_rel'] == 10
    assert after['rel'] is None
    assert after['review']['current']['id'] == before['id']
    assert len(after['review']['history']) == 1
