"""Bounded public-catalog lookups, cached independently of library membership."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import re
import threading
import time
from urllib.parse import urlencode, quote, urlsplit
import xml.etree.ElementTree as ET

from . import sources


class CatalogBusy(ValueError):
    pass


class CatalogQueryError(ValueError):
    pass


def safe_links(paper):
    def valid(value):
        try:
            parsed=urlsplit(value)
            return len(value)<=3000 and parsed.scheme in ('http','https') and parsed.hostname and not parsed.username and not parsed.password
        except (TypeError,ValueError):
            return False
    if not valid(paper.get('canonical_url','')):
        paper['canonical_url']='https://doi.org/'+paper['doi'] if paper.get('doi') else ''
    paper['artifacts']=[artifact for artifact in paper.get('artifacts',[]) if valid(artifact.get('url',''))]
    return paper


def arxiv_records(payload):
    root = ET.fromstring(payload)
    records = []
    for entry in root:
        if sources._local_name(entry) != 'entry':
            continue
        reference = sources._first_child_text(entry, 'id').replace('http://', 'https://')
        try:
            identifier = sources._recognize(reference)
        except ValueError:
            continue
        if identifier.kind != 'arxiv':
            continue
        feed = ET.Element('feed'); feed.append(entry)
        paper = sources._arxiv_metadata(ET.tostring(feed), identifier.value)
        revision = sources._ARXIV_NEW_RE.search(reference) or sources._ARXIV_OLD_RE.search(reference)
        versioned = revision.group(0) if revision else identifier.value
        paper['canonical_url'] = 'https://arxiv.org/abs/'+versioned
        paper['artifacts'] = [{'kind':'pdf','url':'https://arxiv.org/pdf/'+versioned+'.pdf'}]
        if paper['title']:
            records.append(paper)
    return records


class Catalog:
    def __init__(self):
        self.cache = OrderedDict()
        self.lock = threading.Lock()
        self.arxiv_lock = threading.Lock()
        self.arxiv_last = -10.
        self.requests = threading.BoundedSemaphore(4)

    def cached(self, key, fetch):
        with self.lock:
            record = self.cache.get(key)
            if record and record[0] > time.monotonic():
                self.cache.move_to_end(key)
                return copy.deepcopy(record[1]), True
        result = fetch()
        if isinstance(result,list): result=[safe_links(paper) for paper in result]
        elif isinstance(result,dict): result=safe_links(result)
        with self.lock:
            self.cache[key] = (time.monotonic()+600, copy.deepcopy(result))
            self.cache.move_to_end(key)
            while len(self.cache)>256:
                self.cache.popitem(last=False)
        return result, False

    def fetch(self, url, arxiv=False):
        if not self.requests.acquire(blocking=False):
            raise CatalogBusy('Search is busy. Try again in a moment.')
        locked = False
        try:
            if arxiv:
                locked = self.arxiv_lock.acquire(blocking=False)
                if not locked or time.monotonic()-self.arxiv_last < 3:
                    raise CatalogBusy('arXiv needs a short pause between searches. Try again in a few seconds.')
                self.arxiv_last = time.monotonic()
            return sources._fetch_url(url, max_bytes=4*1024**2, timeout=8,
                                      headers={'Accept':'application/atom+xml' if arxiv else 'application/json'})
        finally:
            if locked:
                self.arxiv_lock.release()
            self.requests.release()

    def search(self, query, source, mode='all', offset=0):
        if source not in ('crossref','arxiv') or mode not in ('all','title','author'):
            raise CatalogQueryError('Choose a valid search source and mode')
        if not isinstance(query,str) or not 2 <= len(query.strip()) <= 500 or not 0 <= offset <= 200:
            raise CatalogQueryError('Search with 2–500 characters and an offset from 0 to 200')
        query = ' '.join(query.split())
        def lookup():
            if source == 'crossref':
                field = {'all':'query.bibliographic','title':'query.title','author':'query.author'}[mode]
                endpoint = 'https://api.crossref.org/works?'+urlencode({field:query,'rows':20,'offset':offset})
                payload = json.loads(self.fetch(endpoint))
                items = payload.get('message',{}).get('items')
                if not isinstance(items,list):
                    raise ValueError('Crossref returned an invalid search response')
                papers = []
                for item in items:
                    if not isinstance(item,dict): continue
                    doi = sources._clean_doi(str(item.get('DOI','')))
                    if not doi: continue
                    paper = sources._crossref_metadata({'message':item},doi)
                    if paper['title']: papers.append(paper)
                return papers
            words = re.findall(r'[^\W_]+(?:[-.][^\W_]+)*',query,flags=re.UNICODE)[:40]
            if not words: raise CatalogQueryError('Enter a title or author name')
            terms = [('(ti:"'+word+'" OR au:"'+word+'")') if mode=='all' else ('au' if mode=='author' else 'ti')+':"'+word+'"' for word in words]
            endpoint = 'https://export.arxiv.org/api/query?'+urlencode({'search_query':' AND '.join(terms),'start':offset,'max_results':20,'sortBy':'relevance'})
            return arxiv_records(self.fetch(endpoint,arxiv=True))
        return self.cached(('search',source,mode,query.casefold(),offset),lookup)

    def identify(self, value):
        identifier = sources._recognize(value)
        if identifier.kind == 'doi' and identifier.value.startswith('10.48550/arxiv.'):
            identifier = sources._recognize(identifier.value[len('10.48550/arxiv.'):])
        revision = re.search(r'v\d+(?:\.pdf)?(?:[?#].*)?$',value) if identifier.kind=='arxiv' else None
        version = revision.group(0).split('?')[0].split('#')[0].removesuffix('.pdf') if revision else ''
        return identifier.kind, identifier.value+version

    def resolve(self, values):
        if not isinstance(values,list) or not 1 <= len(values) <= 30 or any(not isinstance(v,str) or not 1 <= len(v.strip()) <= 3000 for v in values):
            raise ValueError('Paste 1–30 paper links, DOIs, or arXiv identifiers')
        results = [None]*len(values)
        arxiv, dois = [], []
        for index,value in enumerate(values):
            try:
                kind,identifier = self.identify(value.strip())
                (arxiv if kind=='arxiv' else dois).append((index,identifier))
            except ValueError as error:
                results[index] = {'input':value,'error':str(error)}
        def doi_lookup(item):
            index,identifier = item
            try:
                def lookup():
                    payload=json.loads(self.fetch('https://api.crossref.org/works/'+quote(identifier,safe='')))
                    return sources._crossref_metadata(payload,identifier)
                paper,_ = self.cached(('doi',identifier),lookup)
                return index, {'input':values[index],'paper':paper}
            except Exception as error:
                return index, {'input':values[index],'error':str(error),**({'retry_after':3} if isinstance(error,CatalogBusy) else {})}
        def arxiv_lookup():
            identifiers = list(dict.fromkeys(identifier for _,identifier in arxiv))
            try:
                papers,_ = self.cached(('arxiv-ids',tuple(identifiers)),lambda:arxiv_records(self.fetch('https://export.arxiv.org/api/query?'+urlencode({'id_list':','.join(identifiers),'max_results':len(identifiers)}),arxiv=True)))
                found = {paper['canonical_url'].rsplit('/',1)[-1]:paper for paper in papers}
                stable = {paper['arxiv_id']:paper for paper in papers}
                return [(index,{'input':values[index],'paper':found.get(identifier) if re.search(r'v\d+$',identifier) else stable.get(identifier)}) for index,identifier in arxiv]
            except Exception as error:
                return [(index,{'input':values[index],'error':str(error),**({'retry_after':3} if isinstance(error,CatalogBusy) else {})}) for index,_ in arxiv]
        with ThreadPoolExecutor(max_workers=4) as pool:
            tasks = [pool.submit(doi_lookup,item) for item in dois]
            arxiv_task = pool.submit(arxiv_lookup) if arxiv else None
            for task in tasks:
                index,result = task.result(); results[index] = result
            if arxiv_task:
                for index,result in arxiv_task.result(): results[index] = result
        for result in results:
            if not result.get('paper') and not result.get('error'):
                result['error'] = 'No metadata found for this identifier.'
        return results
