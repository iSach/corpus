"""Evidence boundaries, review preparation, and decisions across the real API."""
import json

import pytest
from fastapi.testclient import TestClient

from corpus.app import create_app
from corpus.config import Settings
from corpus.db import connection
from corpus import jobs, sources
from test_api import _pdf_fixture
from test_inbox import library, proposal, ingest, decision, TOKEN


def assessment(**changes):
    return {'stage':'abstract','priority':'high','role':'evaluation','confidence':'moderate',
            'explanation':'The abstract reports transfer across physical regimes.',
            'limitation':'The information supplied to the model is unclear.',
            'questions':[], **changes}


def paper(client, pid):
    return client.get('/api/paper',params={'id':pid}).json()


def prepare_pdf(client, settings, monkeypatch, pid):
    monkeypatch.setattr(sources,'safe_download',lambda *args,**kwargs:_pdf_fixture())
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


def test_legacy_importer_suggestion_is_not_an_abstract_assessment(library):
    client, _ = library
    pid=ingest(client,proposal())['results'][0]['id']
    review=paper(client,pid)['review']
    assert review['current']['assessor']=='not-assessed'
    assert review['current']['priority']==review['current']['confidence']=='unknown'
    assert review['check'] is None
    assert client.get('/api/inbox',params={'evidence':'unassessed'}).json()['total']==1
    assert client.get('/api/inbox',params={'evidence':'abstract'}).json()['total']==0


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


def test_abstract_cannot_include_full_text_and_batch_rejection_is_atomic(library):
    client,_=library
    bad=proposal('bad',assessment=assessment(sections=['Methods']))
    good=proposal('good',title='A distinct study of turbulent ocean circulation',assessment=assessment(priority='medium'))
    response=client.post('/api/ingest',headers={'Authorization':'Bearer '+TOKEN},json={'papers':[bad,good]})
    assert response.status_code==200,response.text
    assert response.json()['rejected']==response.json()['accepted']==1
    assert client.get('/api/inbox').json()['total']==1


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


def test_versioned_abstract_cannot_prepare_different_pdf_version(library):
    client,_=library
    pid=ingest(client,proposal(canonical_url='https://arxiv.org/abs/2501.12345v2',
        artifacts=[{'kind':'pdf','url':'https://arxiv.org/pdf/2501.12345v1'}],assessment=assessment(source_version='v2')))['results'][0]['id']
    review=paper(client,pid)['review']
    assert review['check']['status']=='unavailable'
    assert 'v2' in review['check']['error']


def test_source_edit_invalidates_check_but_notes_do_not(library,monkeypatch):
    client,settings=library
    pid=ingest(client,proposal(assessment=assessment()))['results'][0]['id']
    old=prepare_pdf(client,settings,monkeypatch,pid)
    client.post('/api/papers',json={'id':pid,'notes':'My note'})
    assert paper(client,pid)['review']['current']['id']==old['current']['id']
    client.post('/api/papers',json={'id':pid,'abstract':'A revised abstract describing another evaluation.'})
    assert complete(client,pid,old).status_code==409
    current=paper(client,pid)['review']
    assert current['current']['assessor']=='not-assessed' and current['check'] is None
    assert any(entry['id']==old['current']['id'] for entry in current['history'])


def test_approval_records_seen_assessment_even_if_check_just_arrived(library,monkeypatch):
    client,settings=library
    pid=ingest(client,proposal(assessment=assessment()))['results'][0]['id']
    old=paper(client,pid)
    review=prepare_pdf(client,settings,monkeypatch,pid)
    assert complete(client,pid,review).status_code==200
    response=decision(client,pid,'approve',assessment_id=old['review']['current']['id'],expected_modified=old['modified'])
    assert response.status_code==200,response.text
    assert paper(client,pid)['last_decision']['assessment_id']==old['review']['current']['id']
    assert paper(client,pid)['state']=='unreviewed'


def test_ready_check_after_early_approval_remains_available_to_agent(library,monkeypatch):
    client,settings=library
    pid=ingest(client,proposal(assessment=assessment()))['results'][0]['id']
    original=paper(client,pid)['review']['current']['id']
    assert decision(client,pid,'approve',assessment_id=original).status_code==200
    review=prepare_pdf(client,settings,monkeypatch,pid)
    work=client.get('/api/review/work').json()['items']
    assert [item['id'] for item in work]==[pid]
    assert complete(client,pid,review).status_code==200
    updated=paper(client,pid)
    assert updated['admission']=='approved' and updated['state']=='unreviewed'
    assert updated['last_decision']['assessment_id']==original
    assert updated['review']['current']['id']!=original


def test_later_persists_can_resume_and_undo_preserves_the_schedule(library):
    client,settings=library
    pid=ingest(client,proposal())['results'][0]['id']
    deferred=decision(client,pid,'defer')
    assert deferred.status_code==200,deferred.text
    deadline=paper(client,pid)['deferred_until']
    assert deadline and client.get('/api/inbox').json()['total']==0
    assert client.get('/api/inbox',params={'status':'later'}).json()['total']==1
    resumed=decision(client,pid,'resume').json()['decision']
    assert client.get('/api/inbox').json()['total']==1
    assert client.post('/api/inbox/undo',json={'decision_id':resumed['id']}).status_code==200
    assert paper(client,pid)['deferred_until']==deadline
    with connection(settings) as db:
        db.execute("UPDATE papers SET deferred_until='2000-01-01T00:00:00Z' WHERE id=?",(pid,))
    assert client.get('/api/inbox').json()['total']==1


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


def test_topic_change_preserves_but_invalidates_old_assessments(library):
    client,_=library
    pid=ingest(client,proposal(assessment=assessment(priority='medium')))['results'][0]['id']
    old=paper(client,pid)['review']['current']
    response=client.post('/api/topics',json={'id':'physics','label':'Physics','description':'Updated research scope','relevance_criteria':'Require dynamics evidence.'})
    assert response.status_code==200
    new=paper(client,pid)['review']
    assert new['current']['assessor']=='not-assessed'
    assert old['id'] in [entry['id'] for entry in new['history']]


@pytest.mark.parametrize('action',['dismiss','approve','defer'])
def test_undo_back_to_pending_resumes_unfinished_full_text_preparation(library,action):
    client,settings=library
    pid=ingest(client,proposal(assessment=assessment()))['results'][0]['id']
    assert paper(client,pid)['review']['check']['status']=='queued'
    decided=decision(client,pid,action)
    assert decided.status_code==200,decided.text
    undone=client.post('/api/inbox/undo',json={'decision_id':decided.json()['decision']['id']})
    assert undone.status_code==200,undone.text
    restored=paper(client,pid)
    assert restored['admission']=='pending' and restored['review']['check']['status']=='queued'
    artifact=jobs.next_artifact(settings)
    assert artifact and artifact['paper_id']==pid


def test_dismissing_cancels_pending_review_download(library,monkeypatch):
    client,settings=library
    pid=ingest(client,proposal(assessment=assessment()))['results'][0]['id']
    artifact=jobs.next_artifact(settings)
    assert decision(client,pid,'dismiss').status_code==200
    downloads=[]
    monkeypatch.setattr(sources,'safe_download',lambda *args,**kwargs:downloads.append(args))
    jobs.fetch_one(settings,artifact)
    assert downloads==[]
