import pytest
from fastapi.testclient import TestClient
from corpus.app import create_app
from corpus.config import Settings
from corpus.query import QueryError,compile_query
from corpus.sources import MetadataFetchError

TOPIC_FIELDS=[{'id':'sbi-pretrain','label':'SBI pretraining'},{'id':'diff-compose','label':'Diffusion composition'},{'id':'unfiled','label':'Unfiled'}]

@pytest.mark.parametrize('query,facets',[(None,{}),([],{}),({},{}),('',[]),('',0),('',False),('',[['a']])])
def test_nontext_queries_and_nondict_facets(query,facets):
    with pytest.raises(QueryError): compile_query(query,facets)

def test_markdown_and_sql_result_bounds(tmp_path):
    settings=Settings(data_dir=tmp_path,dev=True,pdf_jobs=False,fields=TOPIC_FIELDS)
    with TestClient(create_app(settings)) as client:
        result=client.post('/api/papers',json={'title':'Security fixture','notes':'**bold** <script>alert(1)</script> [bad](javascript:alert(1))','field':'sbi-pretrain'})
        assert result.status_code==200
        paper=result.json()['paper']
        assert '<strong>bold</strong>' in paper['notes_html']
        assert '<script>' not in paper['notes_html'] and 'href="javascript:' not in paper['notes_html']
        for sql in ('SELECT zeroblob(100000000)','SELECT randomblob(100000000)',"SELECT printf('%100000000s','x')"):
            assert client.post('/api/sql',json={'sql':sql}).status_code==400
        result=client.post('/api/papers',json={'id':paper['id'],'notes':'x'*190000})
        assert result.status_code==200
        result=client.post('/api/sql',json={'sql':'SELECT notes || notes FROM papers'})
        assert result.status_code==400 and '256 KiB' in result.json()['detail']
        sql='WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<20) SELECT notes FROM c CROSS JOIN papers'
        result=client.post('/api/sql',json={'sql':sql}).json()
        assert result['truncated'] and 0<len(result['rows'])<20

def test_default_library_starts_with_one_generic_field(tmp_path):
    settings=Settings(data_dir=tmp_path,dev=True,pdf_jobs=False)
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/config').json()['fields']==[{'id':'unfiled','label':'Unfiled'}]
        result=client.post('/api/papers',json={'title':'A general paper'})
        assert result.status_code==200
        assert result.json()['paper']['field']=='unfiled'

def test_metadata_transport_status_and_configurable_default_field(tmp_path,monkeypatch):
    from corpus import app as app_module
    def fail(_): raise MetadataFetchError('Source timed out')
    monkeypatch.setattr(app_module,'fetch_metadata',fail)
    settings=Settings(data_dir=tmp_path,dev=True,pdf_jobs=False,fields=[{'id':'physics','label':'Physics'}])
    with TestClient(create_app(settings)) as client:
        result=client.post('/api/metadata',json={'identifier':'1705.07057'})
        assert result.status_code==502
        result=client.post('/api/papers',json={'title':'Custom field fixture'})
        assert result.status_code==200 and result.json()['paper']['field']=='physics'
