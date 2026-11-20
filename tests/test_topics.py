import json

import pytest
from fastapi.testclient import TestClient

from corpus.app import create_app
from corpus.config import ROOT, Settings


def test_research_topic_persists_and_organizes_papers(tmp_path):
    settings = Settings(data_dir=tmp_path, dev=True, pdf_jobs=False)
    topic = json.loads((ROOT / 'topics/uncertainty-calibration.json').read_text())
    topic['symbol'] = 'moon'
    topic['color'] = 'sky'
    with TestClient(create_app(settings)) as client:
        response = client.post('/api/topics', json=topic)
        assert response.status_code == 200, response.text
        assert response.json()['topic'] == topic
        assert client.post('/api/topics', json=topic).status_code == 200
        assert client.get('/api/topics').json()['topics'] == [
            {'id':'unfiled', 'label':'Unfiled', 'description':'', 'relevance_criteria':'', 'symbol':'', 'color':''}, topic,
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
        topic = {'id':'private','label':'Private','description':'An unpublished research plan.','relevance_criteria':'Private screening criteria.', 'symbol':'star', 'color':'purple'}
        assert client.post('/api/topics', json=topic).status_code == 200
        assert topic in client.get('/api/topics').json()['topics']
        assert client.post('/api/topics', json=topic, headers={'Origin':'https://elsewhere.example'}).status_code == 403
        client.post('/api/logout')
        config = client.get('/api/config')
        assert all(set(field) == {'id','label','symbol','color'} for field in config.json()['fields'])
        assert topic['description'] not in config.text


@pytest.mark.parametrize('changes', [
    {'id':''}, {'id':'bad topic id'}, {'id':'x'*81}, {'id':'../topic'},
    {'label':''}, {'label':'x'*121}, {'description':[]}, {'description':'x'*20001},
    {'relevance_criteria':[]}, {'relevance_criteria':'x'*20001}, {'symbol':'unknown'}, {'symbol':[]}, {'color':'chartreuse'}, {'color':[]},
])
def test_invalid_topics_do_not_change_the_library(tmp_path, changes):
    settings = Settings(data_dir=tmp_path, dev=True, pdf_jobs=False)
    with TestClient(create_app(settings)) as client:
        response = client.post('/api/topics', json={'id':'physics','label':'Physics',**changes})
        assert response.status_code == 400, response.text
        assert len(client.get('/api/topics').json()['topics']) == 1


def test_topic_icon_is_kept_when_a_save_omits_it(tmp_path):
    settings = Settings(data_dir=tmp_path, dev=True, pdf_jobs=False)
    with TestClient(create_app(settings)) as client:
        assert client.post('/api/topics', json={'id':'optics','label':'Optics','symbol':'ring','color':'orange'}).status_code == 200
        assert client.post('/api/topics', json={'id':'optics','label':'Optics, renamed'}).status_code == 200
        saved = next(t for t in client.get('/api/topics').json()['topics'] if t['id'] == 'optics')
        assert (saved['label'], saved['symbol'], saved['color']) == ('Optics, renamed', 'ring', 'orange')


def test_color_migration_keeps_the_color_each_shape_used_to_have(tmp_path):
    import sqlite3
    from corpus.config import ROOT
    db = sqlite3.connect(':memory:')
    db.execute("CREATE TABLE fields(id TEXT PRIMARY KEY, label TEXT, symbol TEXT NOT NULL DEFAULT '')")
    db.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)')
    db.executemany('INSERT INTO fields VALUES(?,?,?)', [('a','A','triangle'), ('b','B','pentagon'), ('c','C','')])
    db.executescript((ROOT / 'migrations/014_topic_colors.sql').read_text())
    assert db.execute('SELECT id,color FROM fields ORDER BY id').fetchall() == [('a','blue'), ('b','vermillion'), ('c','')]
