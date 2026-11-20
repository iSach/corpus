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
        expect(page.locator('.claude-queue')).to_contain_text('all caught up')
        expect(page.locator('.claude-queue')).to_contain_text('1 this month')
        expect(page.locator('.review-facts')).to_contain_text('Claude · claude-opus-5-5')
        with page.expect_response(lambda response: response.url.endswith('/api/ai') and response.request.method == 'POST'):
            page.locator('.claude-backfill input').check()
        expect(page.locator('.claude-backfill input')).to_be_checked()
        assert page.request.get(site + '/api/ai').json()['backfill'] is True
