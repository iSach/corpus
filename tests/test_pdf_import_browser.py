import base64

import pytest
from playwright.sync_api import expect

from corpus.config import Settings
from corpus.db import initialize
from helpers import TOPIC_FIELDS, open_page, serve
from pdf_builder import TITLE, paper_pdf


def offline(app):
    """No public catalog in tests: every PDF is either saved already or new."""
    app.state.catalog.resolve = lambda values: [{'input': value, 'error': 'No metadata found for this identifier.'} for value in values]
    app.state.catalog.search = lambda *args, **kwargs: ([], False)


@pytest.fixture
def pdf_site(tmp_path):
    settings = Settings(data_dir=tmp_path, dev=True, pdf_jobs=False, fields=TOPIC_FIELDS, ingest_token='browser-ingest-token')
    initialize(settings)
    with serve(lambda origin: settings, offline) as origin:
        yield origin


@pytest.fixture
def page(browser, pdf_site):
    with open_page(browser) as page:
        page.goto(pdf_site)
        expect(page.get_by_role('link', name='Add', exact=True)).to_be_visible()
        yield page


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
