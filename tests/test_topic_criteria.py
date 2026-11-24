"""Claude drafts a topic's relevance criteria from the papers filed under it."""
import json
import time

import pytest

from corpus import ai_review, topic_criteria
from corpus.db import connection
from helpers import decision, ingest, proposal

DRAFT = {'criteria': 'Core: generalization to unseen physical regimes.\nNot relevant:\n- pure image classification',
         'basis': 'Based on three regime-shift papers.'}


@pytest.fixture
def topic(library, monkeypatch):
    client, settings = library
    settings.anthropic_api_key = 'test-secret-never-log'
    with connection(settings) as db:
        db.execute("UPDATE fields SET description='Physical generalization',relevance_criteria='Unseen regimes' WHERE id='physics'")
    for slug, rel in (('one', 9), ('two', 4), ('three', None)):
        pid = ingest(client, proposal(slug, title=f'Paper {slug}', abstract=f'Abstract {slug} about regimes. ' * 3))['results'][0]['id']
        # Agent proposals land in the Inbox; these stand for papers already kept in the library.
        with connection(settings) as db:
            db.execute("UPDATE papers SET admission='approved',rel=? WHERE id=?", (rel, pid))
    pending = ingest(client, proposal('noise', title='Paper noise', abstract='Image classification.'))['results'][0]['id']
    assert decision(client, pending, 'dismiss').status_code == 200
    calls = []

    def fake(settings, prompt, system=None, schema=None):
        calls.append({'prompt': json.loads(prompt), 'system': system, 'schema': schema})
        return DRAFT, {'input_tokens': 9000, 'output_tokens': 400, 'cost_usd': 0.02}
    monkeypatch.setattr(ai_review, 'generate', fake)
    topic_criteria._drafts.clear()
    return client, settings, calls


def finish(client, topic_id='physics'):
    for _ in range(200):
        body = client.get('/api/topics/criteria-draft', params={'id': topic_id}).json()
        if body['status'] != 'running':
            return body
        time.sleep(0.01)
    raise AssertionError('draft never finished')


def test_draft_uses_the_topic_papers_and_leaves_the_topic_unsaved(topic):
    client, settings, calls = topic
    started = client.post('/api/topics/criteria-draft', json={'id': 'physics'})
    assert started.status_code == 200 and started.json()['papers'] == 3 and started.json()['dismissed'] == 1
    done = finish(client)
    assert done['status'] == 'complete' and done['criteria'] == DRAFT['criteria'] and done['basis'] == DRAFT['basis']
    sent = calls[0]
    assert sent['system'] == topic_criteria.SYSTEM and sent['schema'] == topic_criteria.SCHEMA
    assert sent['prompt']['current_criteria'] == 'Unseen regimes'
    assert sent['prompt']['topic'] == {'label': 'Physics', 'description': 'Physical generalization'}
    # Highest-rated first; unscored papers last; dismissed papers kept apart as counter-examples.
    assert [p['title'] for p in sent['prompt']['papers']] == ['Paper one', 'Paper two', 'Paper three']
    assert [p['title'] for p in sent['prompt']['dismissed']] == ['Paper noise']
    assert 'rel' not in sent['prompt']['dismissed'][0]
    # Nothing is saved until the editor saves the topic.
    topics = {t['id']: t for t in client.get('/api/topics').json()['topics']}
    assert topics['physics']['relevance_criteria'] == 'Unseen regimes'
    # The call counts against the shared pacer like a review.
    assert ai_review.pacer(settings).snapshot()['calls_today'] == 1


def test_draft_needs_enough_papers_and_a_configured_claude(topic):
    client, settings, calls = topic
    assert client.post('/api/topics/criteria-draft', json={'id': 'unfiled'}).status_code == 409
    assert client.post('/api/topics/criteria-draft', json={'id': 'missing'}).status_code == 404
    assert client.get('/api/topics/criteria-draft', params={'id': 'unfiled'}).json() == {'status': 'none'}
    settings.anthropic_api_key = ''
    assert client.post('/api/topics/criteria-draft', json={'id': 'physics'}).status_code == 409
    assert not calls


def test_draft_waits_out_a_usage_limit_pause(topic):
    client, settings, calls = topic
    ai_review.pacer(settings).pause(time.time() + 3600, 'Claude usage limit reached')
    assert client.post('/api/topics/criteria-draft', json={'id': 'physics'}).status_code == 200
    done = finish(client)
    assert done['status'] == 'failed' and 'paused' in done['error'] and not calls


def test_usage_limit_during_a_draft_pauses_reviews_too(topic, monkeypatch):
    client, settings, calls = topic

    def limited(settings, prompt, system=None, schema=None):
        raise ai_review.RateLimited('Claude usage limit reached', time.time() + 600)
    monkeypatch.setattr(ai_review, 'generate', limited)
    topic_criteria.start(settings, 'physics', background=False)
    assert topic_criteria.result(settings, 'physics')['status'] == 'failed'
    assert ai_review.pacer(settings).snapshot()['paused_until']


def test_overlong_criteria_are_trimmed_and_empty_ones_fail(topic, monkeypatch):
    client, settings, calls = topic
    long = {'criteria': 'x' * 9000, 'basis': 'b'}
    monkeypatch.setattr(ai_review, 'generate', lambda *a, **k: (long, {}))
    topic_criteria.start(settings, 'physics', background=False)
    assert len(topic_criteria.result(settings, 'physics')['criteria']) == topic_criteria.CRITERIA_CHARS
    monkeypatch.setattr(ai_review, 'generate', lambda *a, **k: ({'criteria': ' ', 'basis': ''}, {}))
    topic_criteria.start(settings, 'physics', background=False)
    assert topic_criteria.result(settings, 'physics') == {'status': 'failed', 'error': 'Claude returned no criteria',
                                                          'papers': 3, 'dismissed': 1, 'model': settings.ai_model}


def test_cli_backend_sends_the_criteria_prompt_and_schema(tmp_path, monkeypatch):
    import subprocess
    from corpus.config import Settings
    seen = {}

    def run(command, **kwargs):
        seen['command'] = command
        return subprocess.CompletedProcess(command, 0, json.dumps({'subtype': 'success', 'structured_output': DRAFT, 'usage': {}}), '')
    monkeypatch.setattr(subprocess, 'run', run)
    settings = Settings(data_dir=tmp_path, dev=True, ai_backend='cli', claude_oauth_token='t')
    output, _ = ai_review.run_cli(settings, '{}', system=topic_criteria.SYSTEM, schema=topic_criteria.SCHEMA)
    command = seen['command']
    assert output == DRAFT
    assert command[command.index('--system-prompt') + 1] == topic_criteria.SYSTEM
    assert json.loads(command[command.index('--json-schema') + 1]) == topic_criteria.SCHEMA
