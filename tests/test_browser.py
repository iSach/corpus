"""Browser regressions against an isolated real server and SQLite database."""
import socket, threading, time
import pytest
import uvicorn
from playwright.sync_api import sync_playwright, expect
from corpus.config import Settings
from corpus.app import create_app
from corpus.db import initialize,connection,add_run,validate_paper,write_paper

@pytest.fixture(scope='module')
def site(tmp_path_factory):
    settings=Settings(data_dir=tmp_path_factory.mktemp('browser-data'),dev=True,pdf_jobs=False)
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
