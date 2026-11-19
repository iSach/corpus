"""Fixtures shared across test files."""
import pytest
from fastapi.testclient import TestClient

from corpus import sources
from corpus.app import create_app
from corpus.config import Settings
from corpus.db import add_run, connection, initialize, validate_paper, write_paper
from helpers import INBOX_FIELDS, INBOX_TOKEN, TOPIC_FIELDS, assessment, open_page, pdf_bytes, serve


@pytest.fixture
def app_client(tmp_path):
    """Dev-mode library with the three legacy topics; yields (client, settings)."""
    settings = Settings(data_dir=tmp_path, dev=True, password='test-password', ingest_token='test-ingest-token',
                        pdf_jobs=False, fields=TOPIC_FIELDS)
    with TestClient(create_app(settings)) as client:
        yield client, settings


@pytest.fixture
def library(tmp_path):
    """Dev-mode library for inbox and review tests; agents post with INBOX_TOKEN."""
    settings = Settings(data_dir=tmp_path, dev=True, pdf_jobs=False, ingest_token=INBOX_TOKEN, fields=INBOX_FIELDS)
    with TestClient(create_app(settings)) as client:
        yield client, settings


# Browser tests share one Chromium; each test gets its own server, database and context.

@pytest.fixture(scope='session')
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        yield browser
        browser.close()


def _seed_three_papers(settings):
    initialize(settings)
    with connection(settings) as db:
        run = add_run(db, 'browser-test', 'Synthetic fixture', 3)
        for i, title in enumerate(('Alpha scientific posterior', 'Beta local diffusion', 'Gamma neural inference')):
            p = validate_paper(db, {
                'title': title, 'authors': ['Raymond, A.', 'Another Scientist with a very long author name'],
                'field': 'diff-compose' if i == 1 else 'sbi-pretrain', 'year': 2025, 'venue': 'ICML',
                'state': 'reviewed' if i == 0 else 'unreviewed', 'rel': 9 if i == 0 else None,
                'notes': 'A **bold** note and <script>alert(1)</script>' if i == 0 else '',
                'canonical_url': f'https://example.org/paper/{i}',
                'artifacts': [{'kind': 'pdf', 'url': f'https://example.org/pdf/{i}'}],
                'proposed_rel': 8 if i == 1 else None, 'proposed_reason': 'Fixture draft' if i == 1 else ''})
            pid = write_paper(db, p, run)
            aid = db.execute('SELECT id FROM artifacts WHERE paper_id=?', (pid,)).fetchone()[0]
            db.execute("UPDATE artifacts SET fetch_status='linked' WHERE id=?", (aid,))
            if i == 0:
                db.execute('INSERT INTO pdf_text(paper_id,artifact_id,page,text) VALUES(?,?,4,?)', (pid, aid,
                           'Here posterior collapse is observed. No <script> element should become HTML.'))
        db.execute('UPDATE ingest_run SET accepted=3 WHERE id=?', (run,))


@pytest.fixture
def site(tmp_path):
    """URL of a live dev server seeded with three papers."""
    settings = Settings(data_dir=tmp_path, dev=True, pdf_jobs=False, fields=TOPIC_FIELDS,
                        ingest_token='browser-ingest-token')
    _seed_three_papers(settings)
    with serve(lambda origin: settings) as origin:
        yield origin


@pytest.fixture
def page(browser, site):
    from playwright.sync_api import expect
    with open_page(browser) as page:
        page.goto(site)
        expect(page.locator('.paper-row')).to_have_count(3)
        expect(page.locator('.detail-title')).to_be_visible()
        yield page


@pytest.fixture
def review_page(tmp_path, browser, monkeypatch):
    """Inbox open on one ingested paper whose PDF download is faked; yields (page, site, paper_id)."""
    from playwright.sync_api import expect
    monkeypatch.setattr(sources, 'safe_download', lambda *args, **kwargs: pdf_bytes())
    settings = Settings(data_dir=tmp_path, dev=True, ingest_token='review-browser-token')
    with serve(lambda origin: settings) as site, open_page(browser, height=1000) as page:
        response = page.request.post(site + '/api/ingest', headers={'Authorization': 'Bearer review-browser-token'},
                                     data={'papers': [{
                                         'title': 'A controlled physical generalization evaluation',
                                         'authors': ['Fixture Author'],
                                         'abstract': 'We evaluate physical generalization under distribution shifts.',
                                         'doi': '10.5555/review-browser',
                                         'artifacts': [{'kind': 'pdf', 'url': 'https://example.org/fixture.pdf'}],
                                         'assessment': assessment()}]})
        assert response.status == 200 and response.json()['queued'] == 1
        pid = response.json()['results'][0]['id']
        page.goto(site + '/?screen=inbox')
        # Use the normal navigation so this also exercises loading the review card.
        page.get_by_role('link', name='Inbox', exact=True).click()
        expect(page.locator('.inbox-paper-title')).to_have_text('A controlled physical generalization evaluation')
        yield page, site, pid
