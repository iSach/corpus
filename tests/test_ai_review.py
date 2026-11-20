"""Claude reviews: queue order, pacing, both backends, and editing boundaries."""
import json
import os
import stat
import sys
import time
from datetime import datetime, timezone

import pytest

from corpus import ai_review
from corpus.db import connection
from helpers import ai_result, assessment, decision, ingest, paper, proposal


def enable(settings):
    # Enabled after TestClient startup, so no background worker runs: tests drive the queue.
    settings.anthropic_api_key = 'test-secret-never-log'


def returns(monkeypatch, result, spent=None):
    calls = []
    def fake(settings, prompt):
        calls.append(json.loads(prompt))
        return result, spent or {'input_tokens': 1200, 'output_tokens': 300, 'cost_usd': 0.01}
    monkeypatch.setattr(ai_review, 'generate', fake)
    return calls


def ready(library, **changes):
    client, settings = library
    enable(settings)
    with connection(settings) as db:
        db.execute("UPDATE fields SET description='Physical generalization',relevance_criteria='Unseen physical regimes' WHERE id='physics'")
    pid = ingest(client, proposal(**changes))['results'][0]['id']
    return client, settings, pid


def review_once(settings):
    job = ai_review.claim(settings)
    assert job
    return job, ai_review.process(settings, job)


def with_full_text(settings, pid, text='We hold out three unseen regimes of the reaction rate and measure error growth.'):
    with connection(settings) as db:
        aid = db.execute("SELECT id FROM artifacts WHERE paper_id=? AND kind='pdf' ORDER BY id LIMIT 1", (pid,)).fetchone()[0]
        db.execute("UPDATE artifacts SET fetch_status='cached',fetched_at='2026-11-20T00:00:00Z' WHERE id=?", (aid,))
        db.execute('INSERT INTO pdf_text(paper_id,artifact_id,page,text) VALUES(?,?,3,?)', (pid, aid, text))
    return aid


def test_inbox_paper_gets_digest_assessment_and_draft_scores(library, monkeypatch):
    client, settings, pid = ready(library)
    before = paper(client, pid)
    assert before['review']['current']['assessor'] == 'not-assessed'
    calls = returns(monkeypatch, ai_result())
    job, outcome = review_once(settings)
    assert outcome == 'complete' and job['paper_id'] == pid and job['evidence'] == 'abstract'
    sent = calls[0]
    assert sent['paper']['abstract'] == 'Generalization to unseen physical regimes.'
    assert sent['topics'] == [{'id': 'physics', 'label': 'Physics', 'description': 'Physical generalization', 'relevance_criteria': 'Unseen physical regimes'}]
    assert sent['full_text'] is None
    after = paper(client, pid)
    claude = after['review']['claude']
    assert claude['evidence'] == 'abstract' and not claude['stale']
    assert claude['result']['digest']['kind'] == 'benchmark'
    assert claude['result']['topics'][0]['label'] == 'Physics'
    # Quality scores need the full text; the abstract only supports relevance.
    assert claude['result']['scores'] == {'cred': None, 'qual': None, 'reimpl': None}
    current = after['review']['current']
    assert current['assessor'] == 'ai:' + settings.ai_model + ':' + ai_review.PROMPT_VERSION
    assert current['priority'] == 'high' and current['stage'] == 'abstract'
    assert after['proposed_rel'] == 8 and after['proposed_cred'] is None
    assert after['proposed_reason'].startswith('Claude draft · ' + settings.ai_model + ' · abstract only')
    assert after['rel'] is None and after['admission'] == 'pending'
    assert ai_review.claim(settings) is None
    with connection(settings) as db:
        row = db.execute('SELECT status,input_tokens,cost_usd,backend FROM ai_review_jobs').fetchone()
    assert tuple(row) == ('complete', 1200, 0.01, 'api')


def test_disabled_without_credentials(library):
    client, settings = library
    ingest(client, proposal())
    assert not ai_review.enabled(settings) and ai_review.claim(settings) is None
    assert client.post('/api/review/ai', json={'id': 'anything'}).status_code == 409
    assert client.get('/api/ai').json()['enabled'] is False
    config = client.get('/api/config').text
    assert 'test-secret' not in config and 'oauth' not in config


def test_subscription_reviews_only_the_owner_library(tmp_path):
    from dataclasses import replace
    from corpus.config import Settings
    owner = Settings(data_dir=tmp_path, claude_oauth_token='sk-ant-oat-test')
    assert ai_review.backend(owner) == 'cli'
    member = replace(owner, data_dir=tmp_path / 'users' / 'x', owner_library=False)
    assert ai_review.backend(member) is None
    assert member.ai_state_file == owner.ai_state_file
    assert ai_review.backend(replace(member, anthropic_api_key='key')) == 'api'
    assert ai_review.backend(replace(owner, ai_reviews=False)) is None


def test_owner_assessment_is_kept_but_digest_is_added(library, monkeypatch):
    client, settings, pid = ready(library)
    shown = paper(client, pid)['review']['current']
    assert client.post('/api/review/assessments', json={'id': pid, **assessment(priority='low'), 'expected_assessment_id': shown['id']}).status_code == 200
    returns(monkeypatch, ai_result())
    assert review_once(settings)[1] == 'complete'
    after = paper(client, pid)
    assert after['review']['current']['assessor'] == 'owner' and after['review']['current']['priority'] == 'low'
    assert after['review']['claude']['result']['digest']['tldr']


@pytest.mark.parametrize('change', ['abstract', 'criteria', 'description', 'dismiss'])
def test_inflight_result_cannot_overwrite_changes(library, monkeypatch, change):
    client, settings, pid = ready(library)
    job = ai_review.claim(settings)
    def edit_then_answer(settings, prompt):
        with connection(settings) as db:
            if change == 'abstract':
                db.execute("UPDATE papers SET abstract='New source text' WHERE id=?", (pid,))
            elif change == 'criteria':
                db.execute("UPDATE fields SET relevance_criteria='Different criteria' WHERE id='physics'")
            elif change == 'description':
                db.execute("UPDATE fields SET description='Changed brief' WHERE id='physics'")
            else:
                db.execute("UPDATE papers SET admission='dismissed' WHERE id=?", (pid,))
        return ai_result(), {'input_tokens': 1, 'output_tokens': 1, 'cost_usd': 0}
    monkeypatch.setattr(ai_review, 'generate', edit_then_answer)
    assert ai_review.process(settings, job) == 'superseded'
    with connection(settings) as db:
        assert not db.execute('SELECT 1 FROM ai_reviews').fetchone()
        assert not db.execute("SELECT 1 FROM review_assessments WHERE assessor LIKE 'ai:%'").fetchone()
    # The changed paper is queued again, unless it was dismissed.
    assert (ai_review.claim(settings) is None) == (change == 'dismiss')


def test_every_topic_is_judged_and_suggestions_are_limited_to_other_topics(library, monkeypatch):
    client, settings, pid = ready(library)
    with connection(settings) as db:
        db.execute("INSERT INTO fields(id,label,description) VALUES('fluids','Fluids','Turbulence surrogates'),('empty','Empty','')")
        db.execute("INSERT INTO paper_projects(paper_id,field_id) VALUES(?,'unfiled')", (pid,))
    calls = returns(monkeypatch, ai_result(topics=('physics',)))
    job, outcome = review_once(settings)
    assert outcome == 'failed'
    assert [t['id'] for t in calls[0]['topics']] == ['physics', 'unfiled']
    # Only topics with a brief are offered for suggestions.
    assert [t['id'] for t in calls[0]['other_topics']] == ['fluids']
    with connection(settings) as db:
        assert db.execute('SELECT error FROM ai_review_jobs').fetchone()[0] == 'Claude did not assess every topic'
        db.execute('UPDATE ai_review_jobs SET next_attempt=0')
    suggestions = [{'topic': 'fluids', 'relevance': 7, 'reason': 'Also tests turbulence.'},
                   {'topic': 'physics', 'relevance': 9, 'reason': 'Already filed.'}, {'topic': 'made-up', 'relevance': 9, 'reason': 'No.'}]
    returns(monkeypatch, ai_result(topics=('unfiled', 'physics'), suggested_topics=suggestions))
    assert review_once(settings)[1] == 'complete'
    result = paper(client, pid)['review']['claude']['result']
    assert [t['topic'] for t in result['topics']] == ['physics', 'unfiled']
    assert result['suggested_topics'] == [{'topic': 'fluids', 'label': 'Fluids', 'relevance': 7, 'reason': 'Also tests turbulence.'}]


def test_library_backfill_reads_full_text_and_keeps_verified_quotes(library, monkeypatch):
    client, settings, pid = ready(library)
    assert decision(client, pid, 'approve').status_code == 200
    with_full_text(settings, pid)
    before = paper(client, pid)
    assert ai_review.claim(settings) is None
    assert client.get('/api/ai').json()['queue'] == {'inbox': 0, 'update': 0, 'library': 0}
    assert client.post('/api/ai', json={'backfill': True}).json()['queue']['library'] == 1
    evidence = [{'page': 3, 'quote': 'We hold out three unseen regimes'}, {'page': 3, 'quote': 'A passage that is not in the paper.'}]
    calls = returns(monkeypatch, ai_result(evidence=evidence))
    job, outcome = review_once(settings)
    assert outcome == 'complete' and job['evidence'] == 'full_text'
    assert calls[0]['full_text'] == {'pages_included': '1-1 of 1', 'text': '[Page 3]\nWe hold out three unseen regimes of the reaction rate and measure error growth.\n'}
    after = paper(client, pid)
    result = after['review']['claude']['result']
    assert result['scores'] == {'cred': 6, 'qual': 7, 'reimpl': 5}
    assert result['evidence'] == evidence[:1]
    current = after['review']['current']
    assert current['stage'] == 'full_text' and current['evidence'] == evidence[:1]
    assert after['review']['check']['status'] == 'checked'
    assert after['proposed_qual'] == 7 and 'full text' in after['proposed_reason']
    assert (after['state'], after['rel'], after['admission']) == (before['state'], before['rel'], 'approved')


def test_full_text_arrival_queues_a_deeper_review(library, monkeypatch):
    client, settings, pid = ready(library)
    returns(monkeypatch, ai_result())
    assert review_once(settings)[1] == 'complete'
    with_full_text(settings, pid)
    assert paper(client, pid)['review']['claude']['stale']
    job = ai_review.claim(settings)
    assert job['evidence'] == 'full_text' and job['rank'] == 0


def test_pacer_spacing_daily_cap_pause_and_restart(tmp_path):
    path = tmp_path / 'ai-queue.json'
    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp()
    pace = ai_review.Pacer(path, 180, 2)
    assert pace.wait(now) == 0
    pace.started(now)
    assert pace.wait(now + 60) == 120
    pace.started(now + 200)
    # Two calls today: wait for midnight UTC.
    assert pace.wait(now + 400) == 12 * 3600 - 400
    pace.back_off('Claude is temporarily unavailable', now)
    pace.back_off('Claude is temporarily unavailable', now)
    assert ai_review.Pacer(path, 180, 0).wait(now) == 30 * 60
    pace.pause(now + 5 * 3600, 'Claude usage limit reached')
    restarted = ai_review.Pacer(path, 180, 0)
    assert restarted.wait(now) == 5 * 3600
    assert restarted.snapshot(now)['reason'] == 'Claude usage limit reached'
    restarted.resume()
    assert restarted.wait(now + 400) == 0


def test_usage_limit_pauses_the_queue_without_using_an_attempt(library, monkeypatch):
    client, settings, pid = ready(library)
    job = ai_review.claim(settings)
    until = time.time() + 3600
    def limited(settings, prompt):
        raise ai_review.RateLimited('Claude usage limit reached', until)
    monkeypatch.setattr(ai_review, 'generate', limited)
    assert ai_review.process(settings, job) == 'paused'
    state = client.get('/api/ai').json()
    assert state['pacing']['reason'] == 'Claude usage limit reached'
    assert abs(state['pacing']['paused_until'] - (until + 120)) < 1
    with connection(settings) as db:
        assert tuple(db.execute('SELECT status,attempts FROM ai_review_jobs').fetchone()) == ('queued', 0)
    assert client.post('/api/ai', json={'resume': True}).json()['pacing']['paused_until'] is None


@pytest.mark.parametrize('message,expected', [
    ('Claude AI usage limit reached|1763654400', datetime(2025, 11, 20, 16, tzinfo=timezone.utc)),
    ("You've hit your limit · resets 3pm (Europe/Brussels)", datetime(2026, 11, 20, 14, tzinfo=timezone.utc)),
    ("You've hit your limit · resets 9am (UTC)", datetime(2026, 11, 21, 9, tzinfo=timezone.utc)),
    ('Usage limit reached', None),
])
def test_reset_time_from_limit_message(message, expected):
    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp()
    assert ai_review._reset_time(message, now) == (expected.timestamp() if expected else None)


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """A stand-in `claude` executable that records its call and prints FAKE_CLAUDE_OUTPUT."""
    script = tmp_path / 'claude'
    script.write_text(f'''#!{sys.executable}
import json, os, sys
open(os.environ['FAKE_CLAUDE_LOG'], 'w').write(json.dumps({{'argv': sys.argv[1:], 'stdin': sys.stdin.read(), 'cwd': os.getcwd(),
    'env': {{k: os.environ.get(k) for k in ('HOME', 'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY')}}}}))
print(os.environ['FAKE_CLAUDE_OUTPUT'])
''')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / 'claude-call.json'
    monkeypatch.setenv('FAKE_CLAUDE_LOG', str(log))
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'must-not-leak')
    def respond(output):
        monkeypatch.setenv('FAKE_CLAUDE_OUTPUT', json.dumps(output))
        return lambda: json.loads(log.read_text())
    return script, respond


def test_cli_backend_runs_claude_without_tools_or_settings(tmp_path, fake_claude):
    from corpus.config import Settings
    script, respond = fake_claude
    settings = Settings(data_dir=tmp_path, anthropic_api_key='', claude_oauth_token='sk-ant-oat-test', claude_bin=str(script))
    called = respond({'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': ai_result(),
                      'total_cost_usd': 0.05, 'usage': {'input_tokens': 10, 'cache_read_input_tokens': 1500, 'output_tokens': 400}})
    output, spent = ai_review.generate(settings, '{"paper": {}}')
    assert output == ai_result() and spent == {'input_tokens': 1510, 'output_tokens': 400, 'cost_usd': 0.05}
    call = called()
    argv = call['argv']
    assert argv[argv.index('--tools') + 1] == '' and argv[argv.index('--setting-sources') + 1] == ''
    assert argv[argv.index('--model') + 1] == 'claude-opus-5-5' and '--no-session-persistence' in argv
    assert json.loads(argv[argv.index('--json-schema') + 1]) == ai_review.SCHEMA
    assert call['stdin'] == '{"paper": {}}'
    assert call['env'] == {'HOME': str(tmp_path / 'claude-home'), 'CLAUDE_CODE_OAUTH_TOKEN': 'sk-ant-oat-test', 'ANTHROPIC_API_KEY': None}
    assert call['cwd'] == str(tmp_path / 'claude-home')


@pytest.mark.parametrize('output,error', [
    ({'subtype': 'success', 'is_error': True, 'api_error_status': 429, 'result': "You've hit your limit · resets 3pm (UTC)"}, ai_review.RateLimited),
    ({'subtype': 'success', 'is_error': True, 'api_error_status': 401, 'result': 'Invalid bearer token'}, ai_review.Unavailable),
    ({'subtype': 'success', 'is_error': True, 'api_error_status': 529, 'result': 'Overloaded'}, ai_review.Unavailable),
    ({'subtype': 'error_max_structured_output_retries', 'is_error': True, 'result': ''}, ai_review.ReviewError),
    ({'subtype': 'success', 'is_error': False, 'result': 'plain text'}, ai_review.ReviewError),
])
def test_cli_errors_are_classified(tmp_path, fake_claude, output, error):
    from corpus.config import Settings
    script, respond = fake_claude
    respond(output)
    with pytest.raises(error):
        ai_review.generate(Settings(data_dir=tmp_path, anthropic_api_key='', claude_oauth_token='t', claude_bin=str(script)), '{}')


def test_missing_cli_backs_off(library, monkeypatch):
    client, settings, pid = ready(library)
    settings.anthropic_api_key = ''
    settings.claude_oauth_token = 'sk-ant-oat-test'
    settings.claude_bin = os.path.join(str(settings.data_dir), 'no-such-claude')
    job = ai_review.claim(settings)
    assert ai_review.process(settings, job) == 'unavailable'
    pacing = client.get('/api/ai').json()['pacing']
    assert pacing['reason'] == 'The Claude CLI is not installed for the Corpus service' and pacing['paused_until']


def test_api_backend_request(tmp_path, monkeypatch):
    import anthropic
    from types import SimpleNamespace
    from corpus.config import Settings
    sent = {}
    class Messages:
        def create(self, **params):
            sent.update(params)
            return SimpleNamespace(stop_reason='end_turn', content=[SimpleNamespace(type='text', text=json.dumps(ai_result()))],
                                   usage=SimpleNamespace(input_tokens=100, cache_creation_input_tokens=0, cache_read_input_tokens=900, output_tokens=500))
    class Client:
        def __init__(self, **options):
            assert options['api_key'] == 'key'
            self.beta = SimpleNamespace(messages=Messages())
    monkeypatch.setattr(anthropic, 'Anthropic', Client)
    output, spent = ai_review.generate(Settings(data_dir=tmp_path, anthropic_api_key='key'), '{"paper": {}}')
    assert output == ai_result()
    assert spent == {'input_tokens': 1000, 'output_tokens': 500, 'cost_usd': (1000 * 4 + 500 * 20) / 1e6}
    assert sent['model'] == 'claude-opus-5-5' and 'tool_choice' not in sent
    assert sent['output_config'] == {'effort': 'high', 'format': {'type': 'json_schema', 'schema': ai_review.SCHEMA}}
    assert sent['fallbacks'] == 'default' and sent['betas'] == ['server-side-fallback-2026-07-01']
    assert sent['system'][0]['cache_control'] == {'type': 'ephemeral'}


def test_retry_queues_a_failed_or_refreshed_review(library, monkeypatch):
    client, settings, pid = ready(library)
    returns(monkeypatch, ai_result())
    assert review_once(settings)[1] == 'complete'
    assert ai_review.claim(settings) is None
    assert client.post('/api/review/ai', json={'id': pid}).json() == {'status': 'queued'}
    assert ai_review.claim(settings)['paper_id'] == pid
