"""Interactive, additive capture: one paper can be filed in several projects."""
import json
import re
from fastapi import HTTPException

from .db import connection, add_run, validate_paper, write_paper, find_duplicate, get_paper
from .catalog import Catalog, CatalogBusy, CatalogQueryError
from .query import fts_literal


def metadata_from_library(db,paper_id):
    row=db.execute('SELECT id,title,authors,venue,year,abstract,canonical_url,doi,arxiv_id FROM papers WHERE id=?',(paper_id,)).fetchone()
    paper=dict(row); paper['authors']=json.loads(paper['authors'])
    paper['artifacts']=[dict(r) for r in db.execute('SELECT kind,url FROM artifacts WHERE paper_id=?',(paper_id,))]
    return paper


def annotate(db, papers):
    for paper in papers:
        existing = None
        if paper.get('id'):
            row=db.execute('SELECT id,admission FROM papers WHERE id=?',(paper['id'],)).fetchone()
            if row:
                existing={'id':row['id'],'admission':row['admission'],'projects':[r[0] for r in db.execute('SELECT field_id FROM paper_projects WHERE paper_id=?',(row['id'],))]}
        for key in ('doi','arxiv_id'):
            if existing: break
            if paper.get(key):
                row=db.execute('SELECT id,admission FROM papers WHERE '+key+'=?',(paper[key],)).fetchone()
                if row:
                    existing={'id':row['id'],'admission':row['admission'],
                              'projects':[r[0] for r in db.execute('SELECT field_id FROM paper_projects WHERE paper_id=?',(row['id'],))]}
                    break
        paper['existing']=existing
    return papers


def capture_one(db, metadata, projects, run_id, query, threshold=.91):
    if not isinstance(metadata,dict): raise ValueError('Each selection must be a paper')
    for flag in ('allow_similar','admit_existing'):
        if flag in metadata and type(metadata[flag]) is not bool: raise ValueError(flag+' must be boolean')
    keep=('title','authors','venue','year','abstract','canonical_url','doi','arxiv_id','artifacts')
    incoming={key:metadata[key] for key in keep if key in metadata}
    incoming.update(projects=projects,field=projects[0],state='unreviewed')
    paper=validate_paper(db,incoming)
    duplicate=find_duplicate(db,paper,threshold)
    if metadata.get('use_existing_id') and (not duplicate or metadata['use_existing_id']!=duplicate['id']):
        raise ValueError('The saved paper changed. Search again before adding its projects.')
    if duplicate:
        existing=get_paper(db,duplicate['id'])
        exact=duplicate['reason'].startswith('Exact ')
        use_existing=metadata.get('use_existing_id')==existing['id']
        if not exact and not use_existing and not metadata.get('allow_similar',False):
            return {'status':'similar','existing':existing,'reason':duplicate['reason']}
        if exact or use_existing:
            if existing['admission']!='approved' and metadata.get('admit_existing') is not True:
                return {'status':'needs_review','existing':existing}
            added=[project for project in projects if project not in existing['projects']]
            db.executemany('INSERT OR IGNORE INTO paper_projects(paper_id,field_id) VALUES(?,?)',[(existing['id'],project) for project in added])
            if added:
                db.execute("UPDATE papers SET modified=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",(existing['id'],))
            if existing['admission']!='approved':
                from .inbox import set_admission
                from .review import current
                evidence=current(db,existing['id'])
                set_admission(db,existing,'approved',assessment_id=evidence['id'] if evidence else None)
            db.execute('INSERT INTO provenance(paper_id,run_id,query,status,reason) VALUES(?,?,?,?,?)',
                       (existing['id'],run_id,query,'manual-filed','Added to selected projects' if added else 'Already in selected projects'))
            return {'status':'filed' if added or existing['admission']!='approved' else 'exists','paper':get_paper(db,existing['id']),'added_projects':added}
    pid=write_paper(db,paper,run_id,query=query)
    return {'status':'added','paper':get_paper(db,pid)}


def install_routes(app,settings,limits):
    catalog=Catalog()
    app.state.catalog=catalog

    @app.get('/api/catalog/search')
    def search(q:str,source:str='crossref',mode:str='all',offset:int=0):
        if not limits.allow('catalog-search',120,60): raise HTTPException(429,'Please pause briefly before searching again')
        if source=='library':
            if mode not in ('all','title','author') or not 2<=len(q.strip())<=500 or not 0<=offset<=200: raise ValueError('Invalid paper search')
            words=re.findall(r'[^\W_]+',q,flags=re.UNICODE)[:40]
            if not words: raise ValueError('Enter a title or author name')
            terms=['('+('title : '+fts_literal(word)+' OR authors : '+fts_literal(word) if mode=='all' else ('authors' if mode=='author' else 'title')+' : '+fts_literal(word))+')' for word in words]
            with connection(settings,True) as db:
                rows=db.execute('SELECT p.id FROM papers p WHERE p.id IN (SELECT paper_id FROM paper_fts WHERE paper_fts MATCH ?) ORDER BY p.year DESC,p.title LIMIT 20 OFFSET ?',(' AND '.join(terms),offset)).fetchall()
                papers=annotate(db,[metadata_from_library(db,row['id']) for row in rows])
            return {'papers':papers,'source':'library','cached':False,'offset':offset,'has_more':len(papers)==20 and offset<200}
        try:
            papers,cached=catalog.search(q,source,mode,offset)
        except CatalogBusy as error:
            raise HTTPException(429,str(error),headers={'Retry-After':'3'})
        except CatalogQueryError as error:
            raise ValueError(str(error))
        except Exception as error:
            raise HTTPException(502,source+' search is unavailable: '+str(error)[:250])
        with connection(settings,True) as db: annotate(db,papers)
        return {'papers':papers,'source':source,'cached':cached,'offset':offset,'has_more':len(papers)>=20}

    @app.post('/api/catalog/resolve')
    def resolve(body:dict):
        if not limits.allow('catalog-resolve',30,60): raise HTTPException(429,'Please pause briefly before looking up more links')
        identifiers=body.get('identifiers')
        if not isinstance(identifiers,list) or not 1<=len(identifiers)<=30 or any(not isinstance(value,str) or not 1<=len(value.strip())<=3000 for value in identifiers):
            raise ValueError('Paste 1–30 paper links, DOIs, or arXiv identifiers')
        results=[None]*len(identifiers); missing=[]
        with connection(settings,True) as db:
            for index,value in enumerate(identifiers):
                try:
                    kind,identifier=catalog.identify(value)
                    key='doi' if kind=='doi' else 'arxiv_id'
                    requested=re.search(r'v\d+$',identifier) if kind=='arxiv' else None
                    identifier=re.sub(r'v\d+$','',identifier) if kind=='arxiv' else identifier
                    row=db.execute('SELECT id,canonical_url FROM papers WHERE '+key+'=?',(identifier,)).fetchone()
                    if row and requested:
                        stored=re.search(r'(v\d+)(?:\.pdf)?(?:[?#]|$)',row['canonical_url'])
                        if not stored or stored[1]!=requested[0]:
                            row=None
                    if row:
                        results[index]={'input':value,'paper':metadata_from_library(db,row['id'])}
                        continue
                except ValueError:
                    pass
                missing.append(index)
        if missing:
            for index,result in zip(missing,catalog.resolve([identifiers[index] for index in missing])):
                results[index]=result
        with connection(settings,True) as db:
            annotate(db,[row['paper'] for row in results if row.get('paper')])
        return {'results':results}

    @app.post('/api/capture')
    def save(body:dict):
        projects=body.get('projects')
        if not isinstance(projects,list) or not 1 <= len(projects) <= 100 or any(not isinstance(p,str) or not p for p in projects):
            raise ValueError('Choose one or more projects')
        projects=list(dict.fromkeys(projects))
        papers=body.get('papers')
        if not isinstance(papers,list) or not 1 <= len(papers) <= 50: raise ValueError('Select 1–50 papers')
        query=body.get('query','Manual capture')
        if not isinstance(query,str) or len(query)>4000: raise ValueError('Search context must be under 4,000 characters')
        with connection(settings) as db:
            db.execute('BEGIN IMMEDIATE')
            for project in projects:
                if not db.execute('SELECT 1 FROM fields WHERE id=?',(project,)).fetchone(): raise ValueError('Unknown project: '+project)
            run=add_run(db,'manual-capture',query,len(papers))
            results=[]
            for index,metadata in enumerate(papers):
                db.execute('SAVEPOINT capture_item')
                try:
                    result=capture_one(db,metadata,projects,run,query,settings.dedupe_threshold)
                    db.execute('RELEASE capture_item')
                    results.append({'index':index,**result})
                except ValueError as error:
                    db.execute('ROLLBACK TO capture_item'); db.execute('RELEASE capture_item')
                    results.append({'index':index,'status':'error','error':str(error)})
            added=sum(row['status']=='added' for row in results)
            db.execute('UPDATE ingest_run SET accepted=?,duplicate=?,rejected=? WHERE id=?',
                       (added,sum(row['status'] in ('filed','exists') for row in results),sum(row['status']=='error' for row in results),run))
            return {'results':results,'added':added}
