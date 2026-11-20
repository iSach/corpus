"""New discoveries must be approved before joining the library or PDF queue."""

from fastapi.testclient import TestClient

from corpus.app import create_app
from corpus.config import Settings
from corpus import jobs, sources
from helpers import INBOX_FIELDS as FIELDS, INBOX_TOKEN as TOKEN, decision, ingest, pdf_bytes, proposal


def test_import_waits_outside_library_exports_stats_and_pdf_queue(library,monkeypatch):
    client, settings = library
    manual = client.post('/api/papers',json={'title':'A manual reference'}).json()['paper']
    result = ingest(client,proposal())
    assert result['accepted'] == result['queued'] == 1
    paper_id = result['results'][0]['id']
    paper = client.get('/api/paper',params={'id':paper_id}).json()
    assert paper['admission'] == 'pending' and paper['state'] == 'unreviewed'
    assert paper['rel'] is None and paper['proposed_rel'] == 9
    assert paper['provenance'][0]['status'] == 'queued'
    assert all(artifact['fetch_status']=='linked' for artifact in paper['artifacts'])
    library_data = client.get('/api/papers').json()
    assert library_data['total'] == library_data['library_total'] == 1
    assert library_data['papers'][0]['id'] == manual['id']
    assert client.get('/api/papers',params={'q':'instability OR tag:regimes'}).json()['total'] == 0
    assert {row['value'] for row in library_data['facets']['field']} == {'unfiled'}
    for fmt in ('json','csv','bibtex'):
        exported = client.post('/api/export',json={'scope':'all','format':fmt}).json()
        assert exported['count'] == 1
        assert paper['title'] not in exported['text']
    assert client.post('/api/export',json={'scope':'selection','ids':[paper_id]}).json()['count'] == 0
    stats = client.get('/api/stats').json()
    assert stats['total'] == 1 and stats['code'] == 0 and stats['pdf_pending'] == 0
    assert stats['inbox'] == {'pending':1,'approved':0,'dismissed':0,'later':0,'new':0}
    assert jobs.next_artifact(settings) is None
    pdf = next(artifact for artifact in paper['artifacts'] if artifact['kind']=='pdf')
    for path in ('cache','retry'):
        assert client.post(f"/api/artifacts/{pdf['id']}/{path}").status_code == 409
    downloads = []
    monkeypatch.setattr(sources,'safe_download',lambda *args,**kwargs:downloads.append(args))
    jobs.fetch_one(settings,pdf)
    assert not downloads


def test_approval_queues_pdf_without_marking_as_read_then_full_text_works(library,monkeypatch):
    client, settings = library
    paper_id = ingest(client,proposal())['results'][0]['id']
    approved = decision(client,paper_id,'approve')
    assert approved.status_code == 200, approved.text
    paper = approved.json()['paper']
    assert paper['admission'] == 'approved' and paper['state'] == 'unreviewed' and paper['rel'] is None
    assert client.get('/api/papers').json()['total'] == 1
    assert client.post('/api/export',json={'scope':'all','format':'json'}).json()['count'] == 1
    artifact = jobs.next_artifact(settings)
    assert artifact and artifact['paper_id'] == paper_id
    pdf = pdf_bytes()
    monkeypatch.setattr(sources,'safe_download',lambda *args,**kwargs:pdf)
    jobs.fetch_one(settings,artifact)
    assert client.get('/api/papers',params={'q':'pdf:"posterior collapse"'}).json()['total'] == 1
    assert client.get('/api/inbox').json()['total'] == 0
    assert client.get('/api/inbox',params={'status':'approved'}).json()['total'] == 1


def test_dismissal_is_remembered_across_imports_and_can_be_restored(library):
    client, _ = library
    paper_id = ingest(client,proposal())['results'][0]['id']
    assert decision(client,paper_id,'dismiss').status_code == 200
    again = ingest(client,proposal(),agent='another-discovery-agent')
    assert again['queued'] == 0 and again['duplicate'] == 1
    assert again['results'][0]['admission'] == 'dismissed'
    assert client.get('/api/inbox').json()['total'] == 0
    assert client.get('/api/inbox',params={'status':'dismissed'}).json()['papers'][0]['id'] == paper_id
    assert client.get('/api/papers').json()['total'] == 0
    assert decision(client,paper_id,'restore').status_code == 200
    assert client.get('/api/inbox').json()['total'] == 1
    assert decision(client,paper_id,'approve').status_code == 200


def test_stale_decisions_and_undo_cannot_clobber_later_changes(library):
    client, _ = library
    paper_id = ingest(client,proposal())['results'][0]['id']
    old = client.get('/api/paper',params={'id':paper_id}).json()
    edited = client.post('/api/papers',json={'id':paper_id,'notes':'My own assessment','admission':'approved'})
    assert edited.status_code == 200
    assert edited.json()['paper']['admission'] == 'pending'
    assert decision(client,paper_id,'approve',expected_modified=old['modified']).status_code == 409
    approved = decision(client,paper_id,'approve').json()['decision']
    client.post('/api/papers',json={'id':paper_id,'notes':'Read and annotated','state':'reviewed'})
    assert client.post('/api/inbox/undo',json={'decision_id':approved['id']}).status_code == 409
    assert client.get('/api/papers').json()['papers'][0]['state'] == 'reviewed'


def test_pending_edits_and_new_pdf_links_cannot_bypass_approval(library):
    client, settings = library
    paper_id = ingest(client,proposal())['results'][0]['id']
    response = client.post('/api/papers',json={'id':paper_id,'admission':'approved','artifacts':[{'kind':'pdf','url':'https://example.org/changed.pdf'}]})
    assert response.status_code == 200, response.text
    assert response.json()['paper']['admission'] == 'pending'
    assert response.json()['paper']['artifacts'][0]['fetch_status'] == 'linked'
    assert jobs.next_artifact(settings) is None


def test_ingest_token_cannot_approve_and_session_is_required(tmp_path):
    settings = Settings(data_dir=tmp_path,password='owner-password',pdf_jobs=False,ingest_token=TOKEN,fields=FIELDS)
    with TestClient(create_app(settings)) as client:
        paper_id = ingest(client,proposal())['results'][0]['id']
        assert client.get('/api/inbox').status_code == 401
        assert decision(client,paper_id,'approve').status_code == 401
        assert client.post('/api/inbox/decision',headers={'Authorization':'Bearer '+TOKEN},json={'id':paper_id,'action':'approve'}).status_code == 401
        client.post('/api/login',json={'password':'owner-password'})
        assert client.post('/api/inbox/decision',headers={'Origin':'https://attacker.example'},json={'id':paper_id,'action':'approve'}).status_code == 403
        assert decision(client,paper_id,'approve').status_code == 200


def test_new_count_tracks_papers_since_inbox_was_opened(library):
    client, _ = library
    ingest(client,proposal('first'))
    assert client.get('/api/stats').json()['inbox']['new'] == 0
    assert client.post('/api/inbox/seen').json()['counts']['new'] == 0
    second = ingest(client,proposal('second',title='A second unseen discovery'))['results'][0]['id']
    ingest(client,proposal('third',title='A third unseen discovery'))
    assert client.get('/api/stats').json()['inbox']['new'] == 2
    assert client.get('/api/inbox').json()['counts']['new'] == 2
    assert decision(client,second,'dismiss').status_code == 200
    assert client.get('/api/stats').json()['inbox']['new'] == 1
    assert client.post('/api/inbox/seen').json()['counts'] == {'pending':2,'approved':0,'dismissed':1,'later':0,'new':0}
