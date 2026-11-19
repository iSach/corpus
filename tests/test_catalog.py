import json
from urllib.parse import urlsplit,parse_qs

from corpus.catalog import Catalog
from corpus import sources
from helpers import paper_body


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


def test_batch_arxiv_resolution_uses_one_call_and_retains_per_link_errors(monkeypatch):
    calls=[]
    def fetch(url,**kwargs):
        calls.append(url)
        if 'arxiv.org' in url:return atom()
        message=crossref_payload()['message']['items'][0]
        return json.dumps({'message':message}).encode()
    monkeypatch.setattr(sources,'_fetch_url',fetch)
    results=Catalog().resolve(['https://arxiv.org/pdf/2501.12345v2.pdf','2501.12345v2','https://publisher.example/doi/10.5555/found','https://example.org/no-identifier'])
    # One arXiv batch, one Crossref lookup, and one read of the unrecognized page.
    assert len(calls)==3
    assert all(results[i]['paper']['canonical_url'].endswith('v2') for i in (0,1))
    assert results[2]['paper']['doi']=='10.5555/found'
    assert 'error' in results[3]


def test_untrusted_catalog_links_are_safe(monkeypatch):
    payload=crossref_payload()
    payload['message']['items'][0]['URL']='javascript:alert(1)'
    payload['message']['items'][0]['link']=[{'URL':'javascript:alert(1)','content-type':'application/pdf'}]
    monkeypatch.setattr(sources,'_fetch_url',lambda *args,**kwargs:json.dumps(payload).encode())
    paper=Catalog().search('test','crossref')[0][0]
    assert paper['canonical_url']=='https://doi.org/10.5555/found' and not paper['artifacts']


def test_one_catalog_failure_does_not_break_other_source(app_client,monkeypatch):
    client,_=app_client
    def fetch(url,**kwargs):
        if 'arxiv.org' in url: raise ValueError('Source offline')
        return json.dumps(crossref_payload()).encode()
    monkeypatch.setattr(sources,'_fetch_url',fetch)
    assert client.get('/api/catalog/search',params={'q':'test','source':'arxiv'}).status_code==502
    assert client.get('/api/catalog/search',params={'q':'test','source':'crossref'}).json()['papers']


def test_saved_identifiers_resolve_locally_without_catalog_requests(app_client,monkeypatch):
    client,_=app_client
    existing=client.post('/api/papers',json=paper_body('local-fast-path')).json()['paper']
    def unexpected(*args,**kwargs): raise AssertionError('A saved identifier must not need a catalog request')
    monkeypatch.setattr(sources,'_fetch_url',unexpected)
    response=client.post('/api/catalog/resolve',json={'identifiers':['https://doi.org/'+existing['doi']]})
    assert response.status_code==200,response.text
    result=response.json()['results'][0]['paper']
    assert result['existing']['id']==existing['id']


def test_explicit_arxiv_revision_never_falls_back_to_a_different_revision(monkeypatch):
    monkeypatch.setattr(sources,'_fetch_url',lambda *args,**kwargs:atom('2501.12345v2'))
    results=Catalog().resolve(['2501.12345v1','2501.12345v2'])
    assert 'error' in results[0] and results[1]['paper']['canonical_url'].endswith('v2')
