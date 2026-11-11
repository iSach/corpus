import json

import pytest
from fastapi.testclient import TestClient

from corpus.app import create_app
from corpus.config import ROOT, Settings
from corpus.db import connection, initialize


def test_research_topic_persists_and_organizes_papers(tmp_path):
    settings = Settings(data_dir=tmp_path, dev=True, pdf_jobs=False)
    topic = json.loads((ROOT / 'topics/uncertainty-calibration.json').read_text())
    with TestClient(create_app(settings)) as client:
        response = client.post('/api/topics', json=topic)
        assert response.status_code == 200, response.text
        assert response.json()['topic'] == topic
        assert client.post('/api/topics', json=topic).status_code == 200
        assert client.get('/api/topics').json()['topics'] == [
            {'id':'unfiled', 'label':'Unfiled', 'description':'', 'relevance_criteria':''}, topic,
        ]
        paper = client.post('/api/papers', json={'title':'Calibration under shift', 'field':topic['id']})
        assert paper.status_code == 200
        assert client.get('/api/papers', params={'q':'field:'+topic['id']}).json()['total'] == 1
        topic['description'] += ' An updated evaluation plan.'
        assert client.post('/api/topics', json=topic).status_code == 200
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/topics').json()['topics'][-1] == topic
        papers = client.get('/api/papers', params={'q':'field:'+topic['id']}).json()
        assert papers['total'] == 1 and papers['papers'][0]['id'] == paper.json()['paper']['id']


def test_topic_descriptions_require_login_and_are_not_in_public_config(tmp_path):
    settings = Settings(data_dir=tmp_path, password='test-password', pdf_jobs=False)
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/topics').status_code == 401
        assert client.post('/api/topics', json={'id':'private','label':'Private'}).status_code == 401
        assert client.post('/api/login', json={'password':'test-password'}).status_code == 200
        topic = {'id':'private','label':'Private','description':'An unpublished research plan.','relevance_criteria':'Private screening criteria.'}
        assert client.post('/api/topics', json=topic).status_code == 200
        assert topic in client.get('/api/topics').json()['topics']
        assert client.post('/api/topics', json=topic, headers={'Origin':'https://elsewhere.example'}).status_code == 403
        client.post('/api/logout')
        config = client.get('/api/config')
        assert all(set(field) == {'id','label'} for field in config.json()['fields'])
        assert topic['description'] not in config.text


@pytest.mark.parametrize('changes', [
    {'id':''}, {'id':'bad topic id'}, {'id':'x'*81}, {'id':'../topic'},
    {'label':''}, {'label':'x'*121}, {'description':[]}, {'description':'x'*20001},
    {'relevance_criteria':[]}, {'relevance_criteria':'x'*20001},
])
def test_invalid_topics_do_not_change_the_library(tmp_path, changes):
    settings = Settings(data_dir=tmp_path, dev=True, pdf_jobs=False)
    with TestClient(create_app(settings)) as client:
        response = client.post('/api/topics', json={'id':'physics','label':'Physics',**changes})
        assert response.status_code == 400, response.text
        assert len(client.get('/api/topics').json()['topics']) == 1


def test_topic_migration_retains_existing_fields_and_descriptions(tmp_path):
    settings = Settings(data_dir=tmp_path, pdf_jobs=False)
    settings.data_dir.mkdir(exist_ok=True)
    with connection(settings) as db:
        for name in ('001_initial.sql', '002_smart_list_facets.sql'):
            db.executescript((ROOT / 'migrations' / name).read_text())
        db.execute('INSERT INTO fields(id,label) VALUES(?,?)', ('existing','Existing topic'))
    initialize(settings)
    with connection(settings) as db:
        assert dict(db.execute('SELECT * FROM fields WHERE id=?', ('existing',)).fetchone()) == {
            'id':'existing','label':'Existing topic','description':'','relevance_criteria':'',
        }
        db.execute('UPDATE fields SET description=? WHERE id=?', ('A retained brief.','unfiled'))
    initialize(settings)
    with connection(settings, True) as db:
        assert db.execute('SELECT description FROM fields WHERE id=?', ('unfiled',)).fetchone()[0] == 'A retained brief.'
        assert db.execute('SELECT count(*) FROM schema_migrations WHERE version=3').fetchone()[0] == 1
