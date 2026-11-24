"""Daily review with an actual server, worker, database, and browser."""
import time

from corpus import ai_review
from corpus.config import Settings
from helpers import ai_result, open_page, serve

from playwright.sync_api import expect


def test_full_text_comparison_and_approval_evidence(review_page):
    page,site,pid=review_page
    expect(page.locator('.review-explanation')).to_have_text('The abstract reports transfer across physical regimes.')
    expect(page.locator('.review-limitation')).to_contain_text('information supplied')
    expect(page.get_by_role('button',name='APPROVE FROM ABSTRACT',exact=True)).to_be_visible()
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        status=page.request.get(site+'/api/paper',params={'id':pid}).json()['review']['check']['status']
        if status=='ready': break
        time.sleep(.1)
    assert status=='ready'
    page.get_by_role('button',name='REFRESH',exact=True).click()
    page.get_by_role('button',name='RECORD FULL-TEXT CHECK',exact=True).click()
    expect(page.locator('.review-page-text')).to_contain_text('posterior collapse')
    page.locator('#assessment-priority').select_option('medium')
    page.locator('#assessment-role').select_option('supporting')
    page.locator('#assessment-confidence').select_option('high')
    page.locator('#assessment-explanation').fill('The checked sections provide supporting diagnostic evidence.')
    page.locator('#assessment-limitation').fill('This fixture does not establish long-horizon accuracy.')
    page.locator('#assessment-sections').fill('Results')
    page.locator('#assessment-quote').fill('posterior collapse')
    page.get_by_role('button',name='SAVE ASSESSMENT',exact=True).click()
    expect(page.locator('.review-explanation')).to_have_text('The checked sections provide supporting diagnostic evidence.')
    expect(page.locator('.evidence-level')).to_have_text('Full text checked')
    page.locator('.review-comparison summary').click()
    expect(page.locator('.abstract-assessment')).to_have_text('The abstract reports transfer across physical regimes.')
    expect(page.locator('.review-change')).to_contain_text('High → Medium')
    page.get_by_role('button',name='REFRESH',exact=True).click()
    expect(page.locator('.abstract-assessment')).to_be_visible()
    page.get_by_role('button',name='APPROVE & NEXT',exact=True).click()
    expect(page.get_by_role('button',name='Approved (1)',exact=True)).to_be_visible()
    approved=page.request.get(site+'/api/paper',params={'id':pid}).json()
    assert approved['admission']=='approved' and approved['state']=='unreviewed'
    assert approved['last_decision']['assessment_id']==approved['review']['current']['id']


def test_claude_review_runs_in_background_and_shows_in_inbox(tmp_path, browser, monkeypatch):
    monkeypatch.setattr(ai_review, 'generate', lambda settings, prompt: (ai_result(topics=('unfiled',)), {'input_tokens': 900, 'output_tokens': 300, 'cost_usd': 0.02}))
    settings = Settings(data_dir=tmp_path, dev=True, pdf_jobs=False, ingest_token='claude-browser-token', anthropic_api_key='fake', ai_poll_interval=0.1)
    with serve(lambda origin: settings) as site, open_page(browser, height=1000) as page:
        response = page.request.post(site + '/api/ingest', headers={'Authorization': 'Bearer claude-browser-token'},
                                     data={'papers': [{'title': 'Regime transfer for surrogate solvers', 'authors': ['Fixture Author'],
                                                       'abstract': 'We test surrogates on unseen regimes.', 'doi': '10.5555/claude-browser'}]})
        pid = response.json()['results'][0]['id']
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not page.request.get(site + '/api/paper', params={'id': pid}).json()['review']['claude']:
            time.sleep(.1)
        page.goto(site + '/?screen=inbox')
        page.get_by_role('link', name='Inbox', exact=True).click()
        expect(page.locator('.claude-tldr')).to_have_text('Maps where reaction-diffusion solvers stop generalizing.')
        expect(page.locator('.claude-topic-score')).to_contain_text('8/10 · high')
        expect(page.locator('.claude-queue')).to_have_count(0)
        expect(page.locator('.review-facts')).to_contain_text('Claude · claude-opus-5-5')
        page.get_by_role('link', name='Claude', exact=True).click()
        expect(page.locator('.claude-state')).to_have_text('All caught up')
        expect(page.locator('.claude-facts')).to_contain_text('claude-opus-5-5 · high effort')
        expect(page.locator('.claude-table').first).to_contain_text('1')
        expect(page.locator('.claude-paper')).to_have_text('Regime transfer for surrogate solvers')
        with page.expect_response(lambda response: '/api/ai' in response.url and response.request.method == 'POST'):
            page.locator('.claude-backfill input').check()
        expect(page.locator('.claude-backfill input')).to_be_checked()
        assert page.request.get(site + '/api/ai').json()['backfill'] is True
        # A pause marks the Claude tab and can be lifted from the page.
        ai_review.pacer(settings).pause(time.time() + 3600, 'usage limit reached')
        page.reload()
        expect(page.get_by_role('link', name='Claude: Claude is paused', exact=True)).to_be_visible()
        expect(page.locator('.claude-state.paused')).to_contain_text('usage limit reached')
        page.get_by_role('button', name='RESUME NOW', exact=True).click()
        expect(page.locator('.claude-state')).to_have_text('All caught up')
        expect(page.get_by_role('link', name='Claude', exact=True)).to_be_visible()


def test_claude_drafts_topic_criteria_into_the_editor(tmp_path, browser, monkeypatch):
    from corpus.db import connection
    drafted = 'Core: surrogate solvers that transfer to unseen regimes.\nNot relevant:\n- image benchmarks'
    monkeypatch.setattr(ai_review, 'generate', lambda settings, prompt, system=None, schema=None:
                        ({'criteria': drafted, 'basis': 'Based on three solver papers.'}, {}))
    settings = Settings(data_dir=tmp_path, dev=True, pdf_jobs=False, ingest_token='criteria-token', anthropic_api_key='fake')
    with serve(lambda origin: settings) as site, open_page(browser, height=1000) as page:
        for i, title in enumerate(('Regime transfer for surrogates', 'Operator learning out of distribution', 'Stability of learned PDE solvers')):
            response = page.request.post(site + '/api/ingest', headers={'Authorization': 'Bearer criteria-token'},
                                         data={'papers': [{'title': title, 'authors': ['Fixture Author'],
                                                           'abstract': 'Surrogates evaluated on unseen regimes.', 'doi': f'10.5555/criteria-{i}'}]})
            assert response.ok
        with connection(settings) as db:
            db.execute("UPDATE papers SET admission='approved'")
            field = db.execute('SELECT field FROM papers LIMIT 1').fetchone()[0]
        page.goto(site + '/?screen=topics')
        page.locator('.topic-name-button').first.click()
        page.get_by_role('button', name='Draft with Claude', exact=True).click()
        expect(page.locator('#topic-criteria')).to_have_value(drafted)
        expect(page.locator('.topic-draft-status')).to_contain_text('Drafted by Claude from 3 papers. Based on three solver papers.')
        # Nothing is stored until the topic is saved.
        assert all(t['relevance_criteria'] == '' for t in page.request.get(site + '/api/topics').json()['topics'])
        page.get_by_role('button', name='Save topic', exact=True).click()
        expect(page.locator('.topic-criteria-status.configured')).to_have_count(1)
        saved = {t['id']: t for t in page.request.get(site + '/api/topics').json()['topics']}
        assert saved[field]['relevance_criteria'] == drafted
