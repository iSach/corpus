from contextlib import contextmanager
from pathlib import Path
import sqlite3, json, re, uuid, hashlib, unicodedata, html, time
from difflib import SequenceMatcher
from markdown_it import MarkdownIt

MARKDOWN = MarkdownIt("commonmark", {"html": False})
from .config import ROOT
from .query import compile_query, fts_literal, SCORES, QueryError

KINDS = ('pdf','code','data','slides','video','other')

def connect(settings, readonly=False):
    if readonly:
        db = sqlite3.connect(settings.database.as_uri()+'?mode=ro',uri=True,timeout=3)
        db.execute('PRAGMA query_only=ON')
    else:
        db = sqlite3.connect(settings.database, timeout=10)
        db.execute('PRAGMA foreign_keys=ON')
    db.row_factory=sqlite3.Row
    return db

@contextmanager
def connection(settings, readonly=False):
    db=connect(settings,readonly)
    try:
        with db: yield db
    finally: db.close()

def initialize(settings):
    settings.data_dir.mkdir(parents=True,exist_ok=True)
    settings.cache_dir.mkdir(parents=True,exist_ok=True)
    with connection(settings) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        applied={r[0] for r in db.execute('SELECT version FROM schema_migrations')}
        for path in sorted((ROOT/'migrations').glob('*.sql')):
            if int(path.name.split('_')[0]) not in applied:
                db.executescript('BEGIN IMMEDIATE;\n'+path.read_text()+'\nCOMMIT;')
        for f in settings.fields:
            db.execute('INSERT INTO fields(id,label) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET label=excluded.label',(f['id'],f['label']))


def normalize_doi(value):
    value = str(value or '').strip().lower()
    for prefix in ('https://doi.org/','http://doi.org/','https://dx.doi.org/','doi:'):
        if value.startswith(prefix): value=value[len(prefix):]
    if value and (not re.match(r'^10\.\d{4,9}/\S+$',value) or len(value)>300): raise ValueError('Invalid DOI')
    return value or None

def normalize_arxiv(value):
    value = str(value or '').strip()
    value = re.sub(r'^https?://(?:www\.)?arxiv.org/(?:abs|pdf)/','',value,flags=re.I)
    value = re.sub(r'^arxiv:\s*','',value,flags=re.I)
    value = re.sub(r'(?:v\d+)?(?:\.pdf)?$','',value)
    if value and not re.fullmatch(r'(?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})',value): raise ValueError('Invalid arXiv identifier')
    return value or None

def url(value):
    from urllib.parse import urlsplit
    value=str(value or '').strip()
    if value:
        p=urlsplit(value)
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password: raise ValueError('Artifact and canonical URLs must use http:// or https:// without credentials')
        if len(value)>3000: raise ValueError('URL is too long')
    return value

def normalized_text(text):
    return ' '.join(re.findall(r'\w+',unicodedata.normalize('NFKD',text).casefold()))

def duplicate_candidates(db, paper, exclude=None, limit=5):
    title=normalized_text(paper.get('title',''))
    if len(title)<6: return []
    authors = paper.get('authors',[])
    if not isinstance(authors,list) or any(not isinstance(a,str) for a in authors): raise ValueError('authors must be an ordered list of names')
    auth = normalized_text(' '.join(authors))
    words = list(dict.fromkeys(title.split()))
    # A broad FTS candidate pass avoids a linear fuzzy scan at library scale.
    words = [w for w in words if len(w)>3][:20] or words[:20]
    matches = db.execute('SELECT p.id,p.title,p.authors FROM papers p JOIN paper_fts f ON f.paper_id=p.id WHERE paper_fts MATCH ? ORDER BY rank LIMIT 300',(' OR '.join(fts_literal(w) for w in words),)).fetchall()
    candidates=[]
    for r in matches:
        if r['id']==exclude: continue
        ts=SequenceMatcher(None,title,normalized_text(r['title'])).ratio()
        other_auth=normalized_text(' '.join(json.loads(r['authors'])))
        aus=SequenceMatcher(None,auth,other_auth).ratio() if auth and other_auth else ts
        similarity=round(ts*.85+aus*.15,4)
        if similarity>=.4: candidates.append({'id':r['id'],'title':r['title'],'similarity':similarity})
    return sorted(candidates,key=lambda c:c['similarity'],reverse=True)[:limit]

def find_duplicate(db,paper,threshold,exclude=None):
    for key in ('doi','arxiv_id'):
        if paper.get(key):
            r=db.execute(f'SELECT id,title FROM papers WHERE {key}=? AND id!=?',(paper[key],exclude or '')).fetchone()
            if r: return {**dict(r),'similarity':1.,'reason':'Exact '+key+' match'}
    candidates=duplicate_candidates(db,paper,exclude)
    if candidates and candidates[0]['similarity']>=threshold:
        return {**candidates[0],'reason':'Title and author similarity exceeds configured threshold'}
    return None


def validate_paper(db, incoming, existing=None, ingest=False):
    if not isinstance(incoming,dict): raise ValueError('Each paper must be an object')
    base = dict(existing or {})
    keys=('title','authors','venue','venue_class','year','abstract','canonical_url','arxiv_id','doi','state','field','starred','notes','proposed_reason',*SCORES,*('proposed_'+s for s in SCORES))
    p={k:incoming.get(k,base.get(k)) for k in keys}
    for key in ('title','venue','abstract','canonical_url','notes','proposed_reason'):
        if p[key] is None: p[key]=''
        if not isinstance(p[key],str): raise ValueError(key+' must be text')
        p[key]=p[key].strip()
    if not p['title'] or len(p['title'])>2000: raise ValueError('A title of 1–2,000 characters is required')
    if len(p['abstract'])>100000 or len(p['notes'])>200000: raise ValueError('Abstract or notes exceeds size limit')
    p['authors']=p['authors'] or []
    if not isinstance(p['authors'],list) or len(p['authors'])>1000 or any(not isinstance(a,str) or len(a)>300 for a in p['authors']): raise ValueError('authors must be an ordered list of names')
    p['authors']=[a.strip() for a in p['authors'] if a.strip()]
    p['doi']=normalize_doi(p['doi']); p['arxiv_id']=normalize_arxiv(p['arxiv_id'])
    p['canonical_url']=url(p['canonical_url'])
    if not p['canonical_url']:
        if p['arxiv_id']: p['canonical_url']='https://arxiv.org/abs/'+p['arxiv_id']
        elif p['doi']: p['canonical_url']='https://doi.org/'+p['doi']
    if p['year'] == '': p['year']=None
    if p['year'] is not None and (type(p['year']) is not int or not 1000 <= p['year'] <= 2200): raise ValueError('Year must be an integer between 1000 and 2200')
    if not p['field']:
        default=db.execute("SELECT id FROM fields ORDER BY (id='unfiled') DESC,rowid LIMIT 1").fetchone()
        p['field']=default[0]
    if not db.execute('SELECT 1 FROM fields WHERE id=?',(p['field'],)).fetchone(): raise ValueError('Unknown field: '+str(p['field']))
    p['state']='unreviewed' if ingest else p['state'] or 'unreviewed'
    if p['state'] not in ('unreviewed','reviewed','archived'): raise ValueError('Invalid review state')
    if p['starred'] not in (True,False,None,0,1): raise ValueError('starred must be boolean')
    p['starred']=int(bool(p['starred']))
    p['venue_class']=p['venue_class'] or ('preprint' if not p['venue'] or 'arxiv' in p['venue'].lower() else 'conference' if any(v in p['venue'].lower() for v in ('neurips','icml','iclr','cvpr','aistats','uai')) else 'journal')
    if p['venue_class'] not in ('conference','preprint','journal','other'): raise ValueError('Invalid venue class')
    proposed=incoming.get('proposed_scores',{})
    if not isinstance(proposed,dict): raise ValueError('proposed_scores must be an object')
    aliases={'rel':'relevance','cred':'credibility','qual':'quality','reimpl':'reimplementation'}
    for s in SCORES:
        p['proposed_'+s]=proposed.get(s,proposed.get(aliases[s],p['proposed_'+s]))
        if ingest: p[s]=None
        for key in (s,'proposed_'+s):
            if p[key] is not None and (type(p[key]) is not int or not 0<=p[key]<=10): raise ValueError(key+' must be an integer 0–10 or null')
    p['proposed_reason']=proposed.get('reason',p['proposed_reason'])
    if not isinstance(p['proposed_reason'],str) or len(p['proposed_reason'])>5000: raise ValueError('Proposed-score reason must be text under 5,000 characters')
    tags=incoming.get('tags',base.get('tags',[]))
    if not isinstance(tags,list) or len(tags)>100 or any(not isinstance(t,str) or not t.strip() or len(t)>100 for t in tags): raise ValueError('tags must be a list of nonempty strings (max 100)')
    p['tags']=list(dict.fromkeys(t.strip() for t in tags))
    artifacts=incoming.get('artifacts',base.get('artifacts',[]))
    if not isinstance(artifacts,list) or len(artifacts)>100: raise ValueError('artifacts must be a list (max 100)')
    p['artifacts']=[]
    for a in artifacts:
        if not isinstance(a,dict) or a.get('kind') not in KINDS: raise ValueError('Invalid artifact kind')
        u=url(a.get('url'))
        if not u: raise ValueError('Artifact URL is required')
        if not any(x['kind']==a['kind'] and x['url']==u for x in p['artifacts']): p['artifacts'].append({'kind':a['kind'],'url':u})
    if not p['artifacts'] and p['arxiv_id'] and 'artifacts' not in incoming:
        p['artifacts']=[{'kind':'pdf','url':'https://arxiv.org/pdf/'+p['arxiv_id']}]
    return p

def add_run(db,agent,query='',proposed=1,extra=None):
    return db.execute('INSERT INTO ingest_run(agent,query,proposed,extra) VALUES(?,?,?,?)',(agent,query,proposed,json.dumps(extra or {},ensure_ascii=False))).lastrowid

def write_paper(db,p,run_id=None,paper_id=None,query='',similarity=0):
    tags, artifacts=p['tags'],p['artifacts']
    cols={k:v for k,v in p.items() if k not in ('tags','artifacts')}
    cols['authors']=json.dumps(cols['authors'],ensure_ascii=False)
    if paper_id:
        db.execute('UPDATE papers SET '+','.join(k+'=?' for k in cols)+",modified=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",[*cols.values(),paper_id])
    else:
        paper_id=p['arxiv_id'] or p['doi'] or str(uuid.uuid4())
        surname=normalized_text(p['authors'][0].split()[-1] if p['authors'] else 'anon').replace(' ','')
        bibkey=f"{surname}{p['year'] or 'nd'}_{hashlib.sha256(paper_id.encode()).hexdigest()[:10]}"
        cols.update(id=paper_id,ingest_run_id=run_id,bibtex_key=bibkey)
        db.execute('INSERT INTO papers('+','.join(cols)+') VALUES('+','.join('?' for _ in cols)+')',list(cols.values()))
        db.execute('INSERT INTO provenance(paper_id,run_id,query,status,similarity,reason) VALUES(?,?,?,?,?,?)',(paper_id,run_id,query,'accepted',similarity,'No duplicate above threshold'))
    db.execute('DELETE FROM paper_tags WHERE paper_id=?',(paper_id,))
    for t in tags:
        db.execute('INSERT OR IGNORE INTO tags(name) VALUES(?)',(t,))
        db.execute('INSERT INTO paper_tags SELECT ?,id FROM tags WHERE name=?',(paper_id,t))
    previous=db.execute('SELECT id,kind,url FROM artifacts WHERE paper_id=?',(paper_id,)).fetchall()
    wanted={(a['kind'],a['url']) for a in artifacts}
    for a in previous:
        if (a['kind'],a['url']) not in wanted: db.execute('DELETE FROM artifacts WHERE id=?',(a['id'],))
    for a in artifacts:
        db.execute('INSERT OR IGNORE INTO artifacts(paper_id,kind,url,fetch_status) VALUES(?,?,?,?)',(paper_id,a['kind'],a['url'],'pending' if a['kind']=='pdf' else 'linked'))
    db.execute('DELETE FROM paper_fts WHERE paper_id=?',(paper_id,))
    db.execute('INSERT INTO paper_fts(paper_id,title,authors,abstract,tags,notes) VALUES(?,?,?,?,?,?)',(paper_id,p['title'],' '.join(p['authors']),p['abstract'],' '.join(tags),p['notes']))
    return paper_id

def paper_dict(row):
    p=dict(row); p['authors']=json.loads(p['authors']); p['starred']=bool(p['starred']); return p

def get_paper(db, paper_id, q=''):
    r=db.execute('SELECT * FROM papers WHERE id=?',(paper_id,)).fetchone()
    if not r: return None
    p=paper_dict(r)
    p['notes_html']=MARKDOWN.render(p['notes'])
    p['tags']=[r[0] for r in db.execute('SELECT t.name FROM tags t JOIN paper_tags pt ON pt.tag_id=t.id WHERE pt.paper_id=? ORDER BY t.name',(paper_id,))]
    p['artifacts']=[dict(r) for r in db.execute('SELECT * FROM artifacts WHERE paper_id=? ORDER BY id',(paper_id,))]
    p['provenance']=[dict(r) for r in db.execute('SELECT pr.run_id,r.agent,pr.query,r.timestamp,pr.status,pr.similarity,pr.reason FROM provenance pr JOIN ingest_run r ON r.id=pr.run_id WHERE pr.paper_id=? ORDER BY pr.id DESC',(paper_id,))]
    terms=compile_query(q).pdf_terms
    p['pdf_hits']=[]
    if terms:
        matches=' OR '.join(fts_literal(t) for t in terms)
        for r in db.execute("SELECT page,snippet(pdf_fts,2,char(1),char(2),' … ',30) AS snippet FROM pdf_fts WHERE paper_id=? AND pdf_fts MATCH ? LIMIT 30",(paper_id,matches)):
            snippet=html.escape(r['snippet']).replace('\x01','<mark>').replace('\x02','</mark>')
            p['pdf_hits'].append({'page':int(r['page']),'snippet':snippet})
    return p

def search(db,q='',facets=None,limit=100,offset=0):
    start=time.perf_counter(); c=compile_query(q,facets)
    # Evaluate the potentially broad FTS predicate once for all facet counts.
    count_sql = 'WITH matched AS MATERIALIZED (SELECT p.id,p.field,p.state,p.venue_class,p.year FROM papers p WHERE '+c.where+') '
    pieces=["SELECT 'total' facet,'' value,count(*) count FROM matched", "SELECT 'library_total','',count(*) FROM papers"]
    for key in ('field','state','venue_class','year'):
        pieces.append(f"SELECT '{key}',cast({key} AS TEXT),count(*) FROM matched WHERE {key} IS NOT NULL GROUP BY {key}")
    pieces.append("SELECT 'has',a.kind,count(DISTINCT m.id) FROM matched m JOIN artifacts a ON a.paper_id=m.id GROUP BY a.kind")
    pieces.append("SELECT 'has','cached',count(*) FROM matched m WHERE EXISTS (SELECT 1 FROM artifacts a WHERE a.paper_id=m.id AND a.kind='pdf' AND a.fetch_status='cached')")
    count_rows=db.execute(count_sql+' UNION ALL '.join(pieces),c.params).fetchall()
    counts={key:[] for key in ('field','state','venue_class','year','has')}
    total=library_total=0
    for r in count_rows:
        if r['facet']=='total': total=r['count']
        elif r['facet']=='library_total': library_total=r['count']
        else: counts[r['facet']].append({'value':r['value'],'count':r['count']})
    summary_columns='id,title,authors,venue,venue_class,year,canonical_url,arxiv_id,doi,added,modified,state,field,starred,rel,cred,qual,reimpl,proposed_rel,proposed_cred,proposed_qual,proposed_reimpl,bibtex_key'
    rows=db.execute('SELECT '+','.join('p.'+k for k in summary_columns.split(','))+' FROM papers p WHERE '+c.where+' ORDER BY '+c.order+' LIMIT ? OFFSET ?',c.params+[limit,offset]).fetchall()
    papers=[paper_dict(r) for r in rows]
    if papers:
        by_id={p['id']:p for p in papers}
        for p in papers: p['artifacts']=[]
        for a in db.execute('SELECT * FROM artifacts WHERE paper_id IN ('+','.join('?' for _ in papers)+')',list(by_id)):
            by_id[a['paper_id']]['artifacts'].append(dict(a))
    return {'papers':papers,'total':total,'library_total':library_total,'facets':counts,'sort':c.sort,'elapsed_ms':round((time.perf_counter()-start)*1000,2)}


def readonly_sql(settings,sql,timeout=0.15,max_rows=1000):
    if not isinstance(sql,str) or not sql.strip() or len(sql)>20000: raise ValueError('SQL must contain 1–20,000 characters')
    start=time.perf_counter()
    with connection(settings,True) as db:
        # mode=ro plus query_only are the write barrier. Authorizer additionally
        # excludes ATTACH, PRAGMA and file/extension helpers even in SELECTs.
        allowed={sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_RECURSIVE}
        def authorize(op,a,b,c,d):
            if op==sqlite3.SQLITE_PRAGMA and a=='data_version' and b is None: return sqlite3.SQLITE_OK  # FTS5's internal read
            if op in allowed and not (op==sqlite3.SQLITE_FUNCTION and (b or '').lower() in ('load_extension','writefile','readfile','zeroblob','randomblob','printf','format')): return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        db.set_authorizer(authorize)
        if hasattr(db,'setlimit'):  # Python 3.11+: also cap allocation inside SQLite.
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,1_000_000)
            db.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH,100)
            db.setlimit(sqlite3.SQLITE_LIMIT_COLUMN,100)
        db.set_progress_handler(lambda: int(time.perf_counter()-start>timeout),1000)
        cursor=db.execute(sql)
        if not cursor.description: raise ValueError('The console requires a SELECT result')
        columns=[d[0] for d in cursor.description]
        rows=[]; size=0; truncated=False
        for r in cursor:
            if len(rows)>=max_rows:
                truncated=True; break
            row={}
            for k,v in dict(r).items():
                if isinstance(v,(str,bytes)) and len(v.encode('utf-8') if isinstance(v,str) else v)>262144: raise ValueError('SQL cell exceeds 256 KiB; select a shorter substring')
                row[k]=v.hex() if isinstance(v,bytes) else v
            size+=len(json.dumps(row,ensure_ascii=False).encode('utf-8'))
            if size>2_000_000:
                truncated=True; break
            rows.append(row)
        return {'columns':columns,'rows':rows,'truncated':truncated,'elapsed_ms':round((time.perf_counter()-start)*1000,2)}
