from corpus import pdf_import
from pdf_builder import TITLE, paper_pdf
from helpers import paper_body


def upload(client, data, name='paper.pdf'):
    return client.post('/api/catalog/pdf', params={'name': name}, content=data, headers={'Content-Type': 'application/pdf'})


def offline(app, monkeypatch, resolve=None, search=None):
    """Stand in for arXiv/Crossref; tests never reach the network."""
    catalog = app.state.catalog
    calls = []
    def fake_resolve(values):
        calls.append(('resolve', values))
        return [resolve(value) if resolve else {'input': value, 'error': 'No metadata found for this identifier.'} for value in values]
    def fake_search(query, source, mode='all', offset=0):
        calls.append(('search', source, mode, query))
        return (search(source, query) if search else []), False
    monkeypatch.setattr(catalog, 'resolve', fake_resolve)
    monkeypatch.setattr(catalog, 'search', fake_search)
    return calls


def test_analysis_reads_layout_title_arxiv_stamp_and_abstract():
    found = pdf_import.analyze(paper_pdf(arxiv='2401.01234v2'))
    assert found['title'] == TITLE
    assert found['arxiv_id'] == '2401.01234' and found['arxiv_version'] == 'v2'
    assert found['year'] == 2024
    assert found['abstract'] == 'We calibrate the confidence of small image classifiers with a held-out set.'


def test_upload_rejects_non_pdfs(app_client):
    client, _ = app_client
    assert upload(client, b'<html>not a pdf</html>').status_code == 400
    assert client.post('/api/catalog/pdf', content=b'{}', headers={'Content-Type': 'application/json'}).status_code == 415


def test_arxiv_stamp_matches_a_saved_paper_and_keeps_the_file(app_client, monkeypatch):
    client, settings = app_client
    calls = offline(client.app, monkeypatch)
    saved = client.post('/api/papers', json=paper_body('stamp', arxiv_id='2401.01234', title='A differently typeset title', artifacts=[])).json()['paper']
    data = paper_pdf(arxiv='2401.01234v2')
    response = upload(client, data)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['match'] == {'source': 'library', 'via': 'arXiv id'}
    assert body['paper']['existing']['id'] == saved['id'] and not calls
    result = client.post('/api/capture', json={'projects': ['diff-compose'], 'papers': [{**body['paper'], 'use_existing_id': saved['id']}]}).json()['results'][0]
    assert result['status'] == 'filed' and result['pdf'] == 'attached'
    pdf = next(a for a in result['paper']['artifacts'] if a['url'].startswith('upload:'))
    assert pdf['fetch_status'] == 'cached'
    served = client.get(f"/api/artifacts/{pdf['id']}/file")
    assert served.status_code == 200 and served.content == data
    assert client.get('/api/papers', params={'q': 'pdf:"held-out set"'}).json()['total'] == 1
    # Uploading it again neither duplicates the file nor the paper.
    again = upload(client, data).json()
    result = client.post('/api/capture', json={'projects': ['diff-compose'], 'papers': [{**again['paper'], 'use_existing_id': saved['id']}]}).json()['results'][0]
    assert result['status'] == 'exists' and result['pdf'] == 'kept-existing'


def test_unknown_paper_becomes_an_entry_from_the_pdf(app_client, monkeypatch):
    client, settings = app_client
    offline(client.app, monkeypatch)
    data = paper_pdf()
    body = upload(client, data, 'workshop.pdf').json()
    assert body['match'] == {'source': 'pdf', 'via': None}
    assert body['paper']['title'] == TITLE and body['paper']['existing'] is None
    result = client.post('/api/capture', json={'projects': ['sbi-pretrain'], 'papers': [body['paper']]}).json()['results'][0]
    assert result['status'] == 'added' and result['pdf'] == 'attached'
    paper = result['paper']
    assert paper['venue_class'] == 'preprint' and paper['abstract'].startswith('We calibrate')
    assert [a['fetch_status'] for a in paper['artifacts']] == ['cached']
    # Editing keeps the imported file, which has no web address.
    edited = client.post('/api/papers', json={'id': paper['id'], 'notes': 'Read it', 'artifacts': [{'kind': a['kind'], 'url': a['url']} for a in paper['artifacts']]})
    assert edited.status_code == 200, edited.text
    assert edited.json()['paper']['artifacts'][0]['fetch_status'] == 'cached'
    # A hand-typed upload name for a file that was never imported is never fetched.
    other = client.post('/api/papers', json=paper_body('typed', artifacts=[{'kind': 'pdf', 'url': 'upload:'+'0'*64}])).json()['paper']
    assert other['artifacts'][0]['fetch_status'] == 'linked'


def test_pdf_uploads_may_exceed_the_json_request_limit(app_client, monkeypatch):
    client, _ = app_client
    offline(client.app, monkeypatch)
    large = paper_pdf(padding=3_000_000)
    assert upload(client, large).status_code == 200
    assert client.post('/api/papers', content=b'x'*3_000_000, headers={'Content-Type': 'application/json'}).status_code == 413
