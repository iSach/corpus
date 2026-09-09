import pytest
from corpus.query import Parser, QueryError, compile_query
from corpus.config import Settings
from corpus.db import initialize,connection,validate_paper,add_run,write_paper,search,get_paper

TOPIC_FIELDS=[{'id':'sbi-pretrain','label':'SBI pretraining'},{'id':'diff-compose','label':'Diffusion composition'},{'id':'unfiled','label':'Unfiled'}]

def test_precedence_and_implicit_and():
    a=Parser('a OR b AND NOT c d').parse()
    assert a.kind=='or' and a.left.value=='a'
    assert a.right.kind=='and' and a.right.left.right.kind=='not'
    assert Parser('(a OR b) c').parse().left.kind=='or'
    assert Parser('NOT NOT a').parse().left.kind=='not'

@pytest.mark.parametrize('query',[
    'a AND','OR a','a OR OR b','()','(a','a)','rel:','rel:>','rel:seven','rel:11',
    'year:1.2','unknown:a','state:pending','is:good','has:book','field:>sbi','"abc','pdf:""',
    'pdf:"a\\q"','sort:nope','a OR sort:rel','NOT sort:rel','sort:rel sort:-added',
    '"field":x','a : : b','>' , '('*41+'a'+')'*41,'x '*301,
])
def test_bad_queries(query):
    with pytest.raises(QueryError): compile_query(query)

def test_bound_values_and_literals():
    q=compile_query('author:"O\'Brien" pdf:"posterior collapse" rel:>=7 sort:-year')
    assert "O'Brien" not in q.where
    assert q.params==['authors : "O\'Brien"','"posterior collapse"',7]
    assert q.order=='p.year DESC NULLS LAST, p.id ASC'
    assert q.pdf_terms==['posterior collapse']
    assert compile_query('"a\\"b"').params==['"a""b"']

@pytest.fixture
def database(tmp_path):
    s=Settings(data_dir=tmp_path,pdf_jobs=False,dev=True,fields=TOPIC_FIELDS)
    initialize(s)
    with connection(s) as db:
        run=add_run(db,'test')
        for title,author,score,state,field in [('Alpha posterior inference','Raymond',9,'reviewed','sbi-pretrain'),('Beta diffusion','Zhang',6,'reviewed','diff-compose'),('Gamma posterior','Smith',None,'unreviewed','sbi-pretrain')]:
            data={'title':title,'authors':[author],'rel':score,'field':field,'state':state,'year':2025,'tags':['score-matching'] if score==9 else [],'notes':'Frozen scientific trunk' if score==6 else '', 'artifacts':[{'kind':'pdf','url':'https://arxiv.org/pdf/1234.56789'}]}
            pid=write_paper(db,validate_paper(db,data),run)
            if score==9:
                aid=db.execute('SELECT id FROM artifacts WHERE paper_id=?',(pid,)).fetchone()[0]
                db.execute('INSERT INTO pdf_text(paper_id,artifact_id,page,text) VALUES(?,?,?,?)',(pid,aid,4,'A <script> example of posterior collapse in the model.'))
        yield db

def titles(db,q='',facets=None): return [p['title'] for p in search(db,q,facets)['papers']]

def test_semantics(database):
    db=database
    assert set(titles(db,'posterior'))=={'Alpha posterior inference','Gamma posterior'}
    assert titles(db,'author:Raymond tag:score-matching rel:>7')==['Alpha posterior inference']
    assert titles(db,'field:sbi-pretrain AND (rel:>7 OR state:unreviewed) sort:rel')==['Alpha posterior inference','Gamma posterior']
    assert set(titles(db,'NOT rel:>7'))=={'Beta diffusion','Gamma posterior'}
    assert titles(db,'trunk')==['Beta diffusion']
    assert titles(db,'pdf:"posterior collapse"')==['Alpha posterior inference']
    assert not titles(db,'pdf:"collapse posterior"')
    assert len(titles(db,'has:pdf'))==3
    assert titles(db,'posterior',{'state':['unreviewed']})==['Gamma posterior']
    assert len(titles(db,'',{'rel':0}))==3
    assert titles(db,'',{'rel':7})==['Alpha posterior inference']

def test_snippets_page_and_escaping(database):
    p=search(database,'pdf:"posterior collapse"')['papers'][0]
    detail=get_paper(database,p['id'],'pdf:"posterior collapse"')
    assert detail['pdf_hits'][0]['page']==4
    assert '<mark>posterior collapse</mark>' in detail['pdf_hits'][0]['snippet']
    assert '<script>' not in detail['pdf_hits'][0]['snippet']
    assert not get_paper(database,p['id'],'NOT pdf:collapse')['pdf_hits']

def test_fts_updates(database):
    from corpus.db import validate_paper,write_paper
    p=search(database,'trunk')['papers'][0]
    old=get_paper(database,p['id'])
    write_paper(database,validate_paper(database,{'notes':'replacement','tags':['fresh']},old),paper_id=p['id'])
    assert not titles(database,'trunk')
    assert titles(database,'replacement tag:fresh')==['Beta diffusion']

@pytest.mark.parametrize('facets',[{'what':['x']},{'rel':11},{'rel':'7'},{'field':'sbi'},{'year':['not-year']},{'has':['thing']}])
def test_invalid_facets(facets):
    with pytest.raises(QueryError): compile_query('',facets)
