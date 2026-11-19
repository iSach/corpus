"""AI transport, durable retry, backfill, and concurrent editing boundaries."""
import io
import json
import urllib.error

import pytest

from corpus import ai_review
from corpus.db import connection
from helpers import ai_result as result, assessment, ingest, paper, proposal


def ready(library):
    client, settings = library
    # Enable only after TestClient startup, so tests control processing explicitly.
    settings.anthropic_api_key='test-secret-never-log'
    with connection(settings) as db:
        db.execute("UPDATE fields SET relevance_criteria='physical generalization' WHERE id='physics'")
    pid = ingest(client,proposal())['results'][0]['id']
    return client, settings, pid


def test_ai_backfills_keyword_match_and_keeps_personal_scores(library,monkeypatch):
    client,settings,pid=ready(library)
    before=paper(client,pid)
    assert before['review']['current']['assessor']=='automatic-topic-match-v1'
    monkeypatch.setattr(ai_review,'generate',lambda settings,snapshot:result())
    job=ai_review.claim(settings)
    assert job['paper_id']==pid
    assert ai_review.claim(settings) is None
    ai_review.process(settings,job)
    after=paper(client,pid)
    assert after['review']['current']['assessor']=='ai:anthropic:'+settings.ai_model+':abstract-v1'
    assert after['proposed_rel']==8 and after['proposed_cred'] is None
    assert 'abstract only' in after['proposed_reason']
    assert after['rel'] is None and after['admission']=='pending' and after['state']=='unreviewed'
    assert after['review']['ai']['status']=='complete'
    assert before['review']['current'] in [{**entry,'snapshot':before['review']['current']['snapshot']} for entry in after['review']['history']]
    assert ai_review.claim(settings) is None
    assert client.get('/api/inbox').status_code==200


def test_ai_disabled_without_key(library):
    client,settings=library
    ingest(client,proposal())
    assert ai_review.claim(settings) is None
    assert client.post('/api/review/ai',json={'id':'anything'}).status_code==409
    assert 'anthropic_api_key' not in client.get('/api/config').text


def test_ai_preserves_owner_or_agent_review(library):
    client,settings,pid=ready(library)
    before=paper(client,pid)['review']['current']
    response=client.post('/api/review/assessments',json={'id':pid,**assessment(), 'expected_assessment_id':before['id']})
    assert response.status_code==200
    assert ai_review.claim(settings) is None
    assert client.post('/api/review/ai',json={'id':pid}).status_code==409


@pytest.mark.parametrize('change',['abstract','criteria','owner','dismiss','description'])
def test_inflight_result_cannot_overwrite_changes(library,monkeypatch,change):
    client,settings,pid=ready(library)
    job=ai_review.claim(settings)
    if change=='owner':
        response=client.post('/api/review/assessments',json={'id':pid,**assessment(), 'expected_assessment_id':job['expected_id']})
        assert response.status_code==200
    else:
        with connection(settings) as db:
            if change=='abstract':
                db.execute("UPDATE papers SET abstract='New source text' WHERE id=?",(pid,))
            elif change=='criteria':
                db.execute("UPDATE fields SET relevance_criteria='Different criteria' WHERE id='physics'")
            elif change=='description':
                db.execute("UPDATE fields SET description='Changed brief' WHERE id='physics'")
            else:
                db.execute("UPDATE papers SET admission='dismissed' WHERE id=?",(pid,))
    monkeypatch.setattr(ai_review,'generate',lambda settings,snapshot:result())
    ai_review.process(settings,job)
    with connection(settings) as db:
        assert db.execute('SELECT status FROM ai_review_jobs WHERE id=?',(job['id'],)).fetchone()[0]=='superseded'
        assert not db.execute("SELECT 1 FROM review_assessments WHERE assessor LIKE 'ai:%'").fetchone()


def test_anthropic_request_and_structured_response(library,monkeypatch):
    client,settings,pid=ready(library)
    job=ai_review.claim(settings)
    def respond(request,timeout):
        assert request.full_url=='https://api.anthropic.com/v1/messages'
        assert request.get_header('X-api-key')==settings.anthropic_api_key
        assert timeout==90
        payload=json.loads(request.data)
        assert payload['model']==settings.ai_model
        assert payload['tool_choice']=={'type':'tool','name':'submit_review'}
        assert 'untrusted data' in payload['system']
        supplied=json.loads(payload['messages'][0]['content'])
        assert supplied['abstract']==job['snapshot']['abstract']
        return io.BytesIO(json.dumps({'stop_reason':'tool_use','content':[{'type':'tool_use','name':'submit_review','input':result()}]}).encode())
    monkeypatch.setattr(ai_review.urllib.request,'urlopen',respond)
    assert ai_review.generate(settings,job['snapshot'])==result()


def test_provider_error_does_not_expose_body(library,monkeypatch):
    client,settings,pid=ready(library)
    def fail(*args,**kwargs):
        raise urllib.error.HTTPError('https://api.anthropic.com/v1/messages',401,'bad',{},io.BytesIO(b'secret body'))
    monkeypatch.setattr(ai_review.urllib.request,'urlopen',fail)
    ai_review.process(settings,ai_review.claim(settings))
    assert paper(client,pid)['review']['ai']['error']=='Anthropic HTTP 401'
