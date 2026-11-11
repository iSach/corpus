"""Daily review with an actual server, worker, database, and browser."""
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import expect

from corpus.app import create_app
from corpus.config import Settings
from corpus import sources
from test_api import _pdf_fixture
from test_browser import browser
from test_review import assessment


@pytest.fixture
def review_page(tmp_path, browser, monkeypatch):
    monkeypatch.setattr(sources,'safe_download',lambda *args,**kwargs:_pdf_fixture())
    settings=Settings(data_dir=tmp_path,dev=True,ingest_token='review-browser-token')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    site=f'http://127.0.0.1:{port}'
    server=uvicorn.Server(uvicorn.Config(create_app(settings),host='127.0.0.1',port=port,log_level='error'))
    thread=threading.Thread(target=server.run,daemon=True); thread.start()
    for _ in range(100):
        if server.started: break
        time.sleep(.02)
    assert server.started
    context=browser.new_context(viewport={'width':1440,'height':1000})
    page=context.new_page(); errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    response=page.request.post(site+'/api/ingest',headers={'Authorization':'Bearer review-browser-token'},data={'papers':[{
        'title':'A controlled physical generalization evaluation','authors':['Fixture Author'],
        'abstract':'We evaluate physical generalization under distribution shifts.',
        'doi':'10.5555/review-browser','artifacts':[{'kind':'pdf','url':'https://example.org/fixture.pdf'}],
        'assessment':assessment(),
    }]})
    assert response.status==200 and response.json()['queued']==1
    pid=response.json()['results'][0]['id']
    page.goto(site+'/?screen=inbox')
    # Use the normal navigation so this also exercises loading the review card.
    page.get_by_role('link',name='Inbox',exact=True).click()
    expect(page.locator('.inbox-paper-title')).to_have_text('A controlled physical generalization evaluation')
    yield page,site,pid
    assert not errors
    context.close(); server.should_exit=True; thread.join(5)


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


def test_later_survives_reload_and_can_return_to_pending(review_page):
    page,site,pid=review_page
    page.get_by_role('button',name='LATER',exact=True).click()
    expect(page.get_by_role('button',name='Later (1)',exact=True)).to_be_visible()
    page.reload()
    page.get_by_role('link',name='Inbox',exact=True).click()
    expect(page.locator('.inbox-item')).to_have_count(0)
    page.get_by_role('button',name='Later (1)',exact=True).click()
    expect(page.locator('.inbox-paper-title')).to_have_text('A controlled physical generalization evaluation')
    page.get_by_role('button',name='RETURN TO PENDING',exact=True).click()
    page.get_by_role('button',name='Pending (1)',exact=True).click()
    expect(page.locator('.inbox-item')).to_have_count(1)
    assert page.request.get(site+'/api/papers').json()['total']==0


def test_abstract_assessment_form_and_mobile_filters(review_page):
    page,site,pid=review_page
    page.get_by_role('button',name='ASSESS ABSTRACT',exact=True).click()
    page.locator('#assessment-priority').select_option('medium')
    page.locator('#assessment-confidence').select_option('low')
    page.locator('#assessment-explanation').fill('Potential supporting work; the experimental protocol is unknown.')
    page.get_by_role('button',name='SAVE ASSESSMENT',exact=True).click()
    expect(page.locator('.review-explanation')).to_have_text('Potential supporting work; the experimental protocol is unknown.')
    expect(page.locator('.evidence-level')).to_have_text('Abstract only')
    page.set_viewport_size({'width':390,'height':844})
    page.get_by_role('button',name='Toggle discovery filters',exact=True).click()
    page.get_by_role('combobox',name='Inbox evidence',exact=True).select_option('abstract')
    expect(page.locator('.inbox-item')).to_have_count(1)
    assert page.evaluate('document.documentElement.scrollWidth')<=390
    page.get_by_role('button',name='APPROVE FROM ABSTRACT',exact=True).click()
    expect(page.get_by_role('button',name='Approved (1)',exact=True)).to_be_visible()


def test_decision_actions_stay_visible_while_reading_long_papers(review_page):
    page,site,pid=review_page
    long_abstract='We study physical generalization across parameters and regimes. '*250
    assert page.request.post(site+'/api/papers',data={'id':pid,'abstract':long_abstract}).status==200
    page.get_by_role('button',name='REFRESH',exact=True).click()
    expect(page.locator('.inbox-abstract')).to_have_text(long_abstract.strip())
    reader=page.locator('.inbox-reading')
    assert reader.evaluate('(node) => node.scrollHeight > node.clientHeight')
    action=page.get_by_role('button',name='APPROVE FROM ABSTRACT',exact=True)
    before=action.bounding_box()
    assert before and before['y']+before['height']<=page.viewport_size['height']
    reader.evaluate('(node) => { node.scrollTop = node.scrollHeight; }')
    after=action.bounding_box()
    assert after and abs(before['y']-after['y'])<1
    expect(action).to_be_visible()


def test_mobile_queue_and_reading_are_separate_views(review_page):
    page,site,pid=review_page
    page.set_viewport_size({'width':390,'height':844})
    expect(page.get_by_role('button',name='Back to papers',exact=True)).to_be_visible()
    expect(page.locator('.inbox-queue')).not_to_be_visible()
    action=page.get_by_role('button',name='APPROVE FROM ABSTRACT',exact=True)
    box=action.bounding_box()
    assert box and box['y']+box['height']<=844
    page.get_by_role('button',name='Back to papers',exact=True).click()
    expect(page.locator('.inbox-queue')).to_be_visible()
    expect(page.locator('.inbox-detail')).not_to_be_visible()
    page.keyboard.press('a')
    assert page.request.get(site+'/api/paper',params={'id':pid}).json()['admission']=='pending'
    page.get_by_role('button',name='Show A controlled physical generalization evaluation',exact=True).click()
    expect(page.locator('.inbox-paper-title')).to_be_visible()
    expect(page.locator('.inbox-queue')).not_to_be_visible()
    page.get_by_role('button',name='Toggle discovery filters',exact=True).click()
    expect(page.get_by_role('combobox',name='Inbox evidence',exact=True)).to_be_visible()
    page.get_by_role('combobox',name='Inbox evidence',exact=True).select_option('abstract')
    expect(page.locator('.inbox-filter-rail')).not_to_be_visible()
    expect(page.locator('.inbox-paper-title')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth')<=390


def test_topic_edits_keep_the_assessment_and_show_the_current_brief(review_page):
    page,site,pid=review_page
    page.get_by_role('link',name='Topics',exact=True).click()
    page.get_by_role('button',name='Edit Unfiled',exact=True).click()
    page.locator('#topic-label').fill('Renamed fixture topic')
    page.locator('#topic-description').fill('An edited fixture brief.')
    page.get_by_role('button',name='SAVE TOPIC',exact=True).click()
    expect(page.get_by_role('heading',name='Renamed fixture topic',exact=True)).to_be_visible()
    page.get_by_role('link',name='Inbox',exact=True).click()
    expect(page.locator('.review-explanation')).to_have_text('The abstract reports transfer across physical regimes.')
    expect(page.locator('.evidence-level')).to_have_text('Abstract only')
    page.locator('.review-criteria summary').click()
    expect(page.locator('.review-criteria-text')).to_have_text('An edited fixture brief.')
