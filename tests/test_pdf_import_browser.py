import base64, socket, threading, time

import pytest
import uvicorn
from playwright.sync_api import expect

from corpus.app import create_app
from corpus.config import Settings
from corpus.db import initialize
from pdf_builder import TITLE, paper_pdf
from test_api import TOPIC_FIELDS
from test_browser import browser


@pytest.fixture(scope='module')
def pdf_site(tmp_path_factory):
    settings = Settings(data_dir=tmp_path_factory.mktemp('pdf-import-data'), dev=True, pdf_jobs=False, fields=TOPIC_FIELDS, ingest_token='browser-ingest-token')
    initialize(settings)
    app = create_app(settings)
    # No public catalog in tests: every PDF is either saved already or new.
    app.state.catalog.resolve = lambda values: [{'input': value, 'error': 'No metadata found for this identifier.'} for value in values]
    app.state.catalog.search = lambda *args, **kwargs: ([], False)
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
    thread = threading.Thread(target=server.run, daemon=True); thread.start()
    for _ in range(100):
        if server.started: break
        time.sleep(.02)
    assert server.started
    yield f'http://127.0.0.1:{port}'
    server.should_exit = True; thread.join(5)


@pytest.fixture
def page(browser, pdf_site):
    context = browser.new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page(); page.set_default_timeout(7000); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(pdf_site)
    expect(page.get_by_role('link', name='Add', exact=True)).to_be_visible()
    yield page
    assert not errors
    context.close()


def drop(page, data, name, target='body'):
    """Drop a file on the page the way a file manager does."""
    page.evaluate("""([encoded, name, target]) => {
      const bytes = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
      const transfer = new DataTransfer();
      transfer.items.add(new File([bytes], name, { type: name.endsWith('.pdf') ? 'application/pdf' : 'text/plain' }));
      const node = document.querySelector(target);
      for (const type of ['dragenter', 'dragover', 'drop']) node.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: transfer }));
    }""", [base64.b64encode(data).decode(), name, target])


def test_import_tab_adds_a_new_paper_with_its_pdf(page, pdf_site):
    page.get_by_role('link', name='Add', exact=True).click()
    page.get_by_role('button', name='Import PDF', exact=True).click()
    page.locator('#capture-pdf').set_input_files([{'name': 'tab.pdf', 'mimeType': 'application/pdf', 'buffer': paper_pdf(title='Tab Imported '+TITLE)}])
    result = page.locator('.capture-result')
    expect(result).to_have_count(1)
    expect(result).to_contain_text('Tab Imported '+TITLE)
    expect(result).to_contain_text('a new entry is made from the PDF')
    page.get_by_role('button', name='ADD SELECTED (1)', exact=True).click()
    expect(page.get_by_role('button', name='Already filed Tab Imported '+TITLE, exact=True)).to_be_disabled()
    saved = page.request.get(pdf_site+'/api/papers', params={'q': 'Tab Imported'}).json()['papers'][0]
    assert saved['artifacts'][0]['fetch_status'] == 'cached'


def test_dropping_a_pdf_anywhere_opens_the_import(page, pdf_site):
    expect(page.locator('.screen-nav a[aria-current="page"]')).not_to_have_text('Add')
    drop(page, b'plain', 'notes.txt')
    expect(page.get_by_text('Only PDF files can be imported')).to_be_visible()
    drop(page, paper_pdf(title='Dropped '+TITLE), 'dropped.pdf')
    expect(page.get_by_role('button', name='Import PDF', exact=True)).to_have_attribute('aria-pressed', 'true')
    expect(page.locator('.capture-result')).to_contain_text('Dropped '+TITLE)
    assert 'screen=add' in page.url
    assert not page.evaluate("document.body.classList.contains('pdf-drop-active')")
