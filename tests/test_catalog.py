import json
from urllib.parse import urlsplit,parse_qs

import pytest
from corpus.catalog import Catalog,CatalogBusy
from corpus import sources
from test_api import app_client,_paper


def crossref_payload():
    return {'message':{'items':[{'DOI':'10.5555/found','title':['Found <i>paper</i>'],
            'author':[{'given':'Ada','family':'Author'}],'published':{'date-parts':[[2026]]},
            'URL':'https://doi.org/10.5555/found','abstract':'<p>Dynamics &amp; transfer.</p>'}]}}


def atom(identifier='2501.12345v2',title='A dynamical model'):
    return ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>https://arxiv.org/abs/'+identifier+'</id>'
            '<title>'+title+'</title><summary>A physical abstract.</summary><published>2025-01-01</published>'
            '<author><name>Research Author</name></author></entry></feed>').encode()


def test_title_author_search_normalization_caching_and_paging(monkeypatch):
    calls=[]
    def fetch(url,**kwargs):
        calls.append(url); return json.dumps(crossref_payload()).encode()
    monkeypatch.setattr(sources,'_fetch_url',fetch)
    catalog=Catalog()
    papers,cached=catalog.search('Ada Author','crossref','author')
    assert not cached and parse_qs(urlsplit(calls[0]).query)['query.author']==['Ada Author']
    assert papers[0]['title']=='Found paper' and papers[0]['abstract']=='Dynamics & transfer.'
    papers[0]['title']='Mutated client object'
    again,cached=catalog.search('Ada Author','crossref','author')
    assert cached and again[0]['title']=='Found paper' and len(calls)==1
    catalog.search('Ada Author','crossref','author',20)
    assert len(calls)==2 and parse_qs(urlsplit(calls[1]).query)['offset']==['20']


def test_arxiv_results_keep_versioned_links_and_escape_query_terms(monkeypatch):
    calls=[]
    monkeypatch.setattr(sources,'_fetch_url',lambda url,**kwargs:(calls.append(url) or atom()))
    papers,cached=Catalog().search('neural "operator"','arxiv','title')
    assert papers[0]['arxiv_id']=='2501.12345'
    assert papers[0]['canonical_url'].endswith('2501.12345v2')
    assert papers[0]['artifacts'][0]['url'].endswith('2501.12345v2.pdf')
    assert parse_qs(urlsplit(calls[0]).query)['search_query']==['ti:"neural" AND ti:"operator"']


def test_batch_arxiv_resolution_uses_one_call_and_retains_per_link_errors(monkeypatch):
    calls=[]
    def fetch(url,**kwargs):
        calls.append(url)
        if 'arxiv.org' in url:return atom()
        message=crossref_payload()['message']['items'][0]
        return json.dumps({'message':message}).encode()
    monkeypatch.setattr(sources,'_fetch_url',fetch)
    results=Catalog().resolve(['https://arxiv.org/pdf/2501.12345v2.pdf','2501.12345v2','https://publisher.example/doi/10.5555/found','https://example.org/no-identifier'])
    assert len(calls)==2
    assert all(results[i]['paper']['canonical_url'].endswith('v2') for i in (0,1))
    assert results[2]['paper']['doi']=='10.5555/found'
    assert 'error' in results[3]


def test_arxiv_spacing_and_cache_do_not_block_cached_queries(monkeypatch):
    monkeypatch.setattr(sources,'_fetch_url',lambda *args,**kwargs:atom())
    catalog=Catalog()
    catalog.search('first','arxiv')
    assert catalog.search('first','arxiv')[1]
    with pytest.raises(CatalogBusy):catalog.search('second','arxiv')


def test_untrusted_catalog_links_are_safe(monkeypatch):
    payload=crossref_payload()
    payload['message']['items'][0]['URL']='javascript:alert(1)'
    payload['message']['items'][0]['link']=[{'URL':'javascript:alert(1)','content-type':'application/pdf'}]
    monkeypatch.setattr(sources,'_fetch_url',lambda *args,**kwargs:json.dumps(payload).encode())
    paper=Catalog().search('test','crossref')[0][0]
    assert paper['canonical_url']=='https://doi.org/10.5555/found' and not paper['artifacts']


def test_cached_results_report_current_membership(app_client,monkeypatch):
    client,_=app_client
    monkeypatch.setattr(sources,'_fetch_url',lambda *args,**kwargs:json.dumps(crossref_payload()).encode())
    response=client.get('/api/catalog/search',params={'q':'Found paper','source':'crossref'})
    assert response.status_code==200,response.text
    assert response.json()['papers'][0]['existing'] is None
    client.post('/api/capture',json={'projects':['sbi-pretrain','diff-compose'],'papers':[response.json()['papers'][0]]})
    again=client.get('/api/catalog/search',params={'q':'Found paper','source':'crossref'}).json()
    assert again['cached'] and set(again['papers'][0]['existing']['projects'])=={'sbi-pretrain','diff-compose'}


def test_one_catalog_failure_does_not_break_other_source(app_client,monkeypatch):
    client,_=app_client
    def fetch(url,**kwargs):
        if 'arxiv.org' in url: raise ValueError('Source offline')
        return json.dumps(crossref_payload()).encode()
    monkeypatch.setattr(sources,'_fetch_url',fetch)
    assert client.get('/api/catalog/search',params={'q':'test','source':'arxiv'}).status_code==502
    assert client.get('/api/catalog/search',params={'q':'test','source':'crossref'}).json()['papers']


@pytest.mark.parametrize('params',[{'q':'x'},{'q':'valid','source':'bad'},{'q':'valid','mode':'bad'},{'q':'valid','offset':-1}])
def test_invalid_search_is_rejected(app_client,params):
    client,_=app_client
    assert client.get('/api/catalog/search',params=params).status_code==400


def test_saved_identifiers_resolve_locally_without_catalog_requests(app_client,monkeypatch):
    client,_=app_client
    existing=client.post('/api/papers',json=_paper('local-fast-path')).json()['paper']
    def unexpected(*args,**kwargs): raise AssertionError('A saved identifier must not need a catalog request')
    monkeypatch.setattr(sources,'_fetch_url',unexpected)
    response=client.post('/api/catalog/resolve',json={'identifiers':['https://doi.org/'+existing['doi']]})
    assert response.status_code==200,response.text
    result=response.json()['results'][0]['paper']
    assert result['existing']['id']==existing['id']


def test_local_search_uses_titles_and_authors_and_reports_projects(app_client):
    client,_=app_client
    created=client.post('/api/papers',json=_paper('local-search',authors=['Ada Lovelace'],projects=['sbi-pretrain','diff-compose'],notes='A private note with a uniquephrase')).json()['paper']
    response=client.get('/api/catalog/search',params={'q':'Ada Lovelace','source':'library','mode':'author'})
    assert response.status_code==200,response.text
    assert response.json()['papers'][0]['existing']['id']==created['id']
    assert set(response.json()['papers'][0]['existing']['projects'])=={'sbi-pretrain','diff-compose'}
    assert not client.get('/api/catalog/search',params={'q':'uniquephrase','source':'library'}).json()['papers']


def test_busy_identifier_lookups_have_a_retry_hint(monkeypatch):
    catalog=Catalog()
    def busy(*args,**kwargs):raise CatalogBusy('Please pause')
    monkeypatch.setattr(catalog,'fetch',busy)
    result=catalog.resolve(['2501.12345'])[0]
    assert result['retry_after']==3 and result['error']=='Please pause'


def test_explicit_arxiv_revision_never_falls_back_to_a_different_revision(monkeypatch):
    monkeypatch.setattr(sources,'_fetch_url',lambda *args,**kwargs:atom('2501.12345v2'))
    results=Catalog().resolve(['2501.12345v1','2501.12345v2'])
    assert 'error' in results[0] and results[1]['paper']['canonical_url'].endswith('v2')


def test_saved_different_revision_does_not_replace_requested_metadata(app_client,monkeypatch):
    client,_=app_client
    existing=client.post('/api/papers',json=_paper('versioned-local',arxiv_id='2501.12345',canonical_url='https://arxiv.org/abs/2501.12345v2')).json()['paper']
    monkeypatch.setattr(sources,'_fetch_url',lambda *args,**kwargs:atom('2501.12345v1'))
    result=client.post('/api/catalog/resolve',json={'identifiers':['2501.12345v1']}).json()['results'][0]['paper']
    assert result['canonical_url'].endswith('v1') and result['existing']['id']==existing['id']
    assert client.get('/api/paper',params={'id':existing['id']}).json()['canonical_url'].endswith('v2')
