"""Browser regressions against an isolated real server and SQLite database."""
import json, socket, threading, time
import pytest
import uvicorn
from playwright.sync_api import sync_playwright, expect
from corpus.config import Settings
from corpus.app import create_app
from corpus.db import initialize,connection,add_run,validate_paper,write_paper

TOPIC_FIELDS=[{'id':'sbi-pretrain','label':'SBI pretraining'},{'id':'diff-compose','label':'Diffusion composition'},{'id':'unfiled','label':'Unfiled'}]

@pytest.fixture
def site(tmp_path_factory):
    settings=Settings(data_dir=tmp_path_factory.mktemp('browser-data'),dev=True,pdf_jobs=False,fields=TOPIC_FIELDS,ingest_token='browser-ingest-token')
    initialize(settings)
    with connection(settings) as db:
        run=add_run(db,'browser-test','Synthetic fixture',3)
        for i,title in enumerate(('Alpha scientific posterior','Beta local diffusion','Gamma neural inference')):
            p=validate_paper(db,{'title':title,'authors':['Raymond, A.','Another Scientist with a very long author name'], 'field':'sbi-pretrain' if i!=1 else 'diff-compose','year':2025,'venue':'ICML','state':'reviewed' if i==0 else 'unreviewed','rel':9 if i==0 else None,'notes':'A **bold** note and <script>alert(1)</script>' if i==0 else '', 'canonical_url':f'https://example.org/paper/{i}','artifacts':[{'kind':'pdf','url':f'https://example.org/pdf/{i}'}], 'proposed_rel':8 if i==1 else None,'proposed_reason':'Fixture draft' if i==1 else ''})
            pid=write_paper(db,p,run)
            aid=db.execute('SELECT id FROM artifacts WHERE paper_id=?',(pid,)).fetchone()[0]
            db.execute("UPDATE artifacts SET fetch_status='linked' WHERE id=?",(aid,))
            if i==0: db.execute('INSERT INTO pdf_text(paper_id,artifact_id,page,text) VALUES(?,?,4,?)',(pid,aid,'Here posterior collapse is observed. No <script> element should become HTML.'))
        db.execute('UPDATE ingest_run SET accepted=3 WHERE id=?',(run,))
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(create_app(settings),host='127.0.0.1',port=port,log_level='error'))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    for _ in range(100):
        if server.started: break
        time.sleep(.02)
    assert server.started
    yield f'http://127.0.0.1:{port}'
    server.should_exit=True; thread.join(5)

@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True,args=['--no-sandbox'])
        yield b
        b.close()

@pytest.fixture
def page(browser,site):
    context=browser.new_context(viewport={'width':1440,'height':900})
    page=context.new_page();page.set_default_timeout(7000);errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(site)
    expect(page.locator('.paper-row')).to_have_count(3)
    expect(page.locator('.detail-title')).to_be_visible()
    yield page
    assert not errors
    context.close()


def test_slow_query_typing_preserves_focus_and_pdf_hits(page):
    query=page.get_by_role('searchbox',name='Search query')
    query.press_sequentially('Alpha',delay=220)
    expect(query).to_have_value('Alpha')
    expect(query).to_be_focused()
    expect(page.locator('.paper-row')).to_have_count(1)
    expect(page.locator('.detail-title')).to_have_text('Alpha scientific posterior')
    query.fill('pdf:"posterior collapse"')
    expect(page.locator('.pdf-hit mark')).to_have_text('posterior collapse')
    assert 'p.4' in page.locator('.pdf-hit').inner_text()
    query.fill('field:')
    expect(page.locator('.notice')).to_contain_text('Expected a value')
    expect(page.locator('.paper-row')).to_have_count(1)
    assert page.locator('.detail-pane script').count()==0


def test_keyboard_view_and_palette(page):
    first=page.locator('.paper-row.selected').get_attribute('data-id')
    page.locator('.paper-row.selected').click()
    page.keyboard.press('j')
    expect(page.locator('.paper-row.selected')).to_be_visible()
    page.keyboard.press('x')
    page.get_by_role('button',name='VIEW: ROWS',exact=True).click()
    expect(page.get_by_role('button',name='VIEW: BARS',exact=True)).to_be_visible()
    page.reload()
    expect(page.get_by_role('button',name='VIEW: BARS',exact=True)).to_be_visible()
    page.keyboard.press('Control+k')
    palette=page.get_by_role('searchbox',name='Command palette')
    # The palette input may be a generic text input, depending on semantic type.
    if not palette.count(): palette=page.get_by_role('textbox',name='Command palette')
    palette.press_sequentially('Stats',delay=80)
    expect(palette).to_have_value('Stats')
    palette.press('Enter')
    expect(page.locator('.stats-grid')).to_be_visible()
    assert page.locator('.table-footer').count()==0


def test_sql_console_and_saved_sql_list(page):
    page.get_by_role('button',name='SQL',exact=True).click()
    page.get_by_role('textbox',name='SQL console',exact=True).fill('SELECT title,rel FROM papers ORDER BY title')
    page.get_by_role('button',name='RUN ⏎',exact=True).click()
    expect(page.locator('.sql-result')).to_contain_text('Alpha scientific posterior')
    page.get_by_role('textbox',name='Smart list name').fill('Fixture SQL')
    page.get_by_role('button',name='RUN + SAVE',exact=True).click()
    page.keyboard.press('Control+k')
    page.get_by_role('button',name='Fixture SQL sql',exact=True).click()
    expect(page.locator('.sql-result')).to_contain_text('Alpha scientific posterior')
    expect(page.locator('.notice')).to_have_count(0)


def test_triage_save_and_export(page,site):
    page.get_by_role('searchbox',name='Search query').fill('Beta')
    expect(page.locator('.paper-row')).to_have_count(1)
    expect(page.locator('.detail-title')).to_have_text('Beta local diffusion')
    page.get_by_role('button',name='EDIT',exact=True).click()
    expect(page.locator('.entry-form')).to_be_visible()
    page.get_by_role('button',name='ACCEPT DRAFT SCORES',exact=True).click()
    page.locator('#form-notes').fill('Triaged in browser')
    page.get_by_role('button',name='SAVE & NEXT UNREVIEWED',exact=False).click()
    expect(page.locator('[data-focus-key="form-title"]')).to_have_value('Gamma neural inference')
    response=page.request.get(site+'/api/papers',params={'q':'Beta state:reviewed rel:8'})
    assert response.json()['total']==1
    page.get_by_role('link',name='Export',exact=True).click()
    page.get_by_label('whole library',exact=False).check()
    page.get_by_role('button',name='JSON',exact=True).click()
    expect(page.locator('.preview')).to_contain_text('Triaged in browser')
    with page.expect_download() as download:
        page.get_by_role('button',name='DOWNLOAD .json',exact=True).click()
    assert download.value.suggested_filename=='corpus.json'


def test_identifier_capture_keeps_pdf_and_review_inputs(page,site):
    metadata={
        'title':'Physical regime forecasting from a short context',
        'authors':['Research Author'], 'year':2025, 'venue':'arXiv',
        'abstract':'Transfer across dynamical regimes. <script>alert(1)</script>',
        'canonical_url':'https://arxiv.org/abs/2501.01234',
        'arxiv_id':'2501.01234', 'doi':None,
        'artifacts':[{'kind':'pdf','url':'https://arxiv.org/pdf/2501.01234.pdf'}],
    }
    page.route('**/api/metadata',lambda route:route.fulfill(json=metadata))
    page.get_by_role('link',name='Add',exact=True).click()
    page.get_by_role('button',name='Manual entry',exact=True).click()
    page.locator('#form-identifier').fill('2501.01234')
    page.get_by_role('group',name='Paper projects').get_by_role('checkbox',name='SBI pretraining',exact=True).uncheck()
    page.get_by_role('group',name='Paper projects').get_by_role('checkbox',name='Diffusion composition',exact=True).check()
    page.locator('#form-state').select_option('reviewed')
    page.locator('#form-starred').check()
    page.locator('#form-notes').fill('Check stability and growth rates')
    page.locator('#form-tags').fill('ML, ml, regimes')
    page.get_by_role('button',name='+ ARTIFACT',exact=True).click()
    page.locator('#artifact-0-kind').select_option('code')
    page.locator('#artifact-0-url').fill('https://example.org/solver')
    page.get_by_role('button',name='FETCH',exact=True).click()
    expect(page.locator('#form-title')).to_have_value(metadata['title'])
    expect(page.locator('.artifact-editor-row')).to_have_count(2)
    # Fetching again must not duplicate or replace existing artifacts.
    with page.expect_response('**/api/metadata'):
        page.get_by_role('button',name='FETCH',exact=True).click()
    expect(page.locator('.artifact-editor-row')).to_have_count(2)
    expect(page.get_by_role('group',name='Paper projects').get_by_role('checkbox',name='Diffusion composition',exact=True)).to_be_checked()
    expect(page.locator('#form-state')).to_have_value('reviewed')
    expect(page.locator('#form-starred')).to_be_checked()
    expect(page.locator('#form-notes')).to_have_value('Check stability and growth rates')
    page.get_by_role('button',name='SAVE',exact=True).click()
    expect(page.locator('.detail-title')).to_have_text(metadata['title'])
    expect(page.locator('.detail-abstract')).to_have_text(metadata['abstract'])
    assert page.locator('.detail-abstract script').count()==0
    paper=page.request.get(site+'/api/paper',params={'id':metadata['arxiv_id']}).json()
    assert paper['field']=='diff-compose' and paper['state']=='reviewed' and paper['starred']
    assert set(paper['tags'])=={'ML','regimes'}
    assert len(paper['artifacts'])==2
    pdf=next(artifact for artifact in paper['artifacts'] if artifact['kind']=='pdf')
    assert pdf['url']==metadata['artifacts'][0]['url'] and pdf['fetch_status']=='pending'
    page.reload()
    expect(page.locator('.detail-abstract')).to_have_text(metadata['abstract'])


def test_topics_can_be_created_edited_and_used_for_papers(page,site):
    page.get_by_role('link',name='Topics',exact=True).click()
    page.get_by_role('button',name='+ New topic',exact=True).click()
    page.get_by_label('Name',exact=True).fill('Forecast transfer')
    page.get_by_label('Topic ID',exact=True).fill('forecast-transfer')
    description='Pretrained forecasters: test transfer, stability, and long-horizon behavior.'
    page.get_by_label('Research description',exact=True).fill(description)
    page.get_by_role('button',name='Save topic',exact=True).click()
    expect(page.get_by_role('heading',name='Forecast transfer',exact=True)).to_be_visible()
    expect(page.locator('.topic-description').filter(has_text=description)).to_be_visible()
    page.reload()
    expect(page.get_by_role('heading',name='Forecast transfer',exact=True)).to_be_visible()
    page.get_by_role('button',name='Edit Forecast transfer',exact=True).click()
    expect(page.get_by_label('Topic ID',exact=True)).to_be_disabled()
    page.get_by_label('Research description',exact=True).fill(description+' Measure long-term dynamics.')
    page.get_by_role('button',name='Save topic',exact=True).click()
    expect(page.locator('.topic-description').filter(has_text='Measure long-term dynamics.')).to_be_visible()
    page.get_by_role('button',name='View papers in Forecast transfer',exact=True).click()
    expect(page.get_by_role('checkbox',name='Forecast transfer 0',exact=True)).to_be_checked()
    expect(page.locator('.paper-row')).to_have_count(0)
    page.get_by_role('link',name='Add',exact=True).click()
    page.get_by_role('button',name='Manual entry',exact=True).click()
    page.locator('#form-title').fill('Generalization across physical regimes')
    projects=page.get_by_role('group',name='Paper projects')
    projects.get_by_role('checkbox',name='Forecast transfer',exact=True).check()
    page.get_by_role('button',name='SAVE',exact=True).click()
    expect(page.locator('.detail-title')).to_have_text('Generalization across physical regimes')
    papers=page.request.get(site+'/api/papers',params={'q':'field:forecast-transfer'}).json()
    assert papers['total']==1 and papers['papers'][0]['field']=='forecast-transfer'


def add_inbox_candidates(page,site):
    candidates=[
        {'title':'Phase boundaries from short observation contexts','authors':['Research Author'],
         'doi':'10.5555/browser-phase','field':'sbi-pretrain','abstract':'Growth, decay, and extinction. <script>alert(1)</script>',
         'artifacts':[{'kind':'pdf','url':'https://example.org/phase.pdf'}],
         'proposed_scores':{'rel':9,'reason':'Evaluates forecasts under unseen parameters.'}},
        {'title':'Composing molecular priors for protein structure','authors':['Another Author'],
         'doi':'10.5555/browser-protein','field':'diff-compose','abstract':'A different scientific application.',
         'proposed_scores':{'rel':6,'reason':'Adjacent scientific modeling application.'}},
    ]
    response=page.request.post(site+'/api/ingest',headers={'Authorization':'Bearer browser-ingest-token'},data={'papers':candidates})
    assert response.status==200 and response.json()['queued']==2
    return [item['id'] for item in response.json()['results']]


def test_inbox_approve_dismiss_undo_and_restore(page,site):
    ids=add_inbox_candidates(page,site)
    page.get_by_role('link',name='Inbox',exact=True).click()
    expect(page.locator('.inbox-item')).to_have_count(2)
    expect(page.locator('.inbox-paper-title')).to_have_text('Phase boundaries from short observation contexts')
    expect(page.locator('.inbox-reason')).to_have_text('Evaluates forecasts under unseen parameters.')
    expect(page.locator('.inbox-abstract')).to_contain_text('<script>alert(1)</script>')
    assert page.locator('.inbox-abstract script').count()==0
    page.get_by_role('button',name='APPROVE FROM ABSTRACT',exact=True).click()
    expect(page.locator('.inbox-paper-title')).to_have_text('Composing molecular priors for protein structure')
    assert page.request.get(site+'/api/papers').json()['total']==4
    approved=page.request.get(site+'/api/paper',params={'id':ids[0]}).json()
    assert approved['state']=='unreviewed' and approved['rel'] is None
    assert approved['artifacts'][0]['fetch_status']=='pending'
    page.get_by_role('button',name='UNDO LAST DECISION',exact=True).click()
    expect(page.locator('.inbox-paper-title')).to_have_text('Phase boundaries from short observation contexts')
    expect(page.locator('.inbox-item')).to_have_count(2)
    assert page.request.get(site+'/api/papers').json()['total']==3
    page.locator('.inbox-paper-title').click()
    page.keyboard.press('d')
    expect(page.locator('.inbox-item')).to_have_count(1)
    expect(page.locator('.inbox-paper-title')).to_have_text('Composing molecular priors for protein structure')
    page.get_by_role('button',name='Dismissed (1)',exact=True).click()
    expect(page.locator('.inbox-paper-title')).to_have_text('Phase boundaries from short observation contexts')
    page.get_by_role('button',name='RESTORE TO INBOX',exact=True).click()
    expect(page.get_by_role('button',name='Pending (2)',exact=True)).to_be_visible()
    page.get_by_role('button',name='Pending (2)',exact=True).click()
    expect(page.locator('.inbox-item')).to_have_count(2)


def test_inbox_filters_pending_edits_and_mobile_layout(page,site):
    ids=add_inbox_candidates(page,site)
    page.get_by_role('link',name='Inbox',exact=True).click()
    expect(page.locator('.inbox-paper-title')).to_have_text('Phase boundaries from short observation contexts')
    page.get_by_role('combobox',name='Inbox topic',exact=True).select_option('diff-compose')
    expect(page.locator('.inbox-item')).to_have_count(1)
    expect(page.locator('.inbox-paper-title')).to_have_text('Composing molecular priors for protein structure')
    page.get_by_role('button',name='EDIT DETAILS',exact=True).click()
    expect(page.locator('#form-title')).to_have_value('Composing molecular priors for protein structure')
    page.locator('#form-notes').fill('Consider as a comparison baseline.')
    page.get_by_role('button',name='SAVE',exact=True).click()
    expect(page.locator('.inbox-paper-title')).to_have_text('Composing molecular priors for protein structure')
    paper=page.request.get(site+'/api/paper',params={'id':ids[1]}).json()
    assert paper['notes']=='Consider as a comparison baseline.' and paper['admission']=='pending'
    assert page.request.get(site+'/api/papers').json()['total']==3
    page.set_viewport_size({'width':390,'height':844})
    page.get_by_role('button',name='APPROVE FROM ABSTRACT',exact=True).scroll_into_view_if_needed()
    expect(page.get_by_role('button',name='APPROVE FROM ABSTRACT',exact=True)).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth')<=390


def test_inbox_refreshes_new_arrivals_without_changing_the_current_paper(page,site):
    add_inbox_candidates(page,site)
    page.clock.install()
    page.reload()
    page.get_by_role('link',name='Inbox',exact=True).click()
    expect(page.locator('.inbox-item')).to_have_count(2)
    expect(page.locator('.inbox-paper-title')).to_have_text('Phase boundaries from short observation contexts')
    response=page.request.post(site+'/api/ingest',headers={'Authorization':'Bearer browser-ingest-token'},data={'papers':[{'title':'Long-term stability of ocean circulation','authors':['Ocean Researcher'],'doi':'10.5555/new-ocean','proposed_scores':{'rel':10}}]})
    assert response.json()['queued']==1
    page.clock.run_for(60000)
    expect(page.locator('.inbox-item')).to_have_count(3)
    expect(page.locator('.inbox-paper-title')).to_have_text('Phase boundaries from short observation contexts')


def test_published_version_check_and_apply(page,site,monkeypatch):
    from corpus import venues
    neurips={'title':'Implicit posterior flows','publication_year':2021,'locations':[
        {'source':{'display_name':'neural information processing systems','type':'conference'},'landing_page_url':'https://proceedings.neurips.cc/paper/2022/hash/abc'}]}
    def fetch(url,headers):
        if 'openalex' in url: return json.dumps(neurips).encode()
        raise venues.sources.SourceError('source returned HTTP 404')
    monkeypatch.setattr(venues,'_http',fetch)  # the server thread shares this process
    created=page.request.post(site+'/api/papers',data={'title':'Implicit posterior flows','authors':['Raymond, A.'],'venue':'arXiv','year':2021,'arxiv_id':'2111.00001','field':'unfiled'})
    assert created.ok
    page.get_by_role('searchbox',name='Search query').fill('Implicit')
    expect(page.locator('.detail-title')).to_have_text('Implicit posterior flows')
    section=page.locator('.detail-section',has_text='PUBLISHED VERSION')
    expect(section).to_contain_text('Not checked yet')
    section.get_by_role('button',name='CHECK NOW').click()
    expect(section.locator('.venue-found')).to_have_text('Advances in Neural Information Processing Systems (NeurIPS) 2022')
    expect(section).to_contain_text('via OpenAlex')
    section.get_by_role('button',name='APPLY').click()
    expect(section).to_contain_text('set by you · preprint arXiv:2111.00001')
    expect(page.locator('.detail-meta')).to_contain_text('(NeurIPS) 2022')
    expect(page.locator('.paper-row.selected')).to_contain_text('NeurIPS')


def test_topics_compact_search_symbols_and_mobile_editor(page, site):
    page.get_by_role('link', name='Topics', exact=True).click()
    expect(page.locator('.topic-row')).to_have_count(3)
    expect(page.locator('.topic-editor')).to_have_count(0)
    search = page.get_by_role('searchbox', name='Search topics')
    search.fill('diffusion')
    expect(page.locator('.topic-row')).to_have_count(1)
    expect(search).to_be_focused()
    search.fill('no match')
    expect(page.get_by_text('No topics match your search.', exact=True)).to_be_visible()
    search.fill('')
    page.get_by_role('button', name='Edit SBI pretraining', exact=True).click()
    icons = page.locator('.symbol-options svg').evaluate_all('(icons) => icons.map(icon => [icon.getBoundingClientRect().width, icon.getBoundingClientRect().height, parseFloat(getComputedStyle(icon).fontSize)])')
    assert len(icons) == 8
    assert all(width == height == font_size for width, height, font_size in icons)
    page.get_by_role('radio', name='Triangle', exact=True).check()
    page.get_by_role('button', name='Save topic', exact=True).click()
    expect(page.locator('.topic-row .symbol-triangle')).to_have_count(1)
    page.reload()
    expect(page.locator('.topic-row .symbol-triangle')).to_have_count(1)
    page.set_viewport_size({'width': 390, 'height': 844})
    page.get_by_role('button', name='Edit SBI pretraining', exact=True).click()
    expect(page.locator('.topic-editor')).to_be_visible()
    expect(page.get_by_role('button', name='Save topic', exact=True)).to_be_in_viewport()
    assert page.evaluate('document.documentElement.scrollWidth') <= 390
    page.get_by_role('radio', name='Star', exact=True).check()
    page.once('dialog', lambda dialog: dialog.dismiss())
    page.get_by_role('button', name='Cancel', exact=True).click()
    expect(page.get_by_role('radio', name='Star', exact=True)).to_be_checked()
    page.get_by_role('button', name='Save topic', exact=True).click()
    expect(page.locator('.topic-row .symbol-star')).to_have_count(1)
    add_inbox_candidates(page, site)
    page.get_by_role('link', name='Inbox', exact=True).click()
    expect(page.locator('.inbox-item .symbol-star')).to_have_count(1)
