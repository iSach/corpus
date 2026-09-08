from contextlib import asynccontextmanager
from collections import defaultdict, deque
from pathlib import Path
import asyncio, hashlib, hmac, json, logging, os, secrets, sqlite3, time
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from .config import Settings, ROOT
from .db import connection, initialize, validate_paper, find_duplicate, duplicate_candidates, add_run, write_paper, get_paper, search, readonly_sql
from .query import QueryError, compile_query
from .exporting import export_papers
from .stats import stats
from .jobs import worker
from .sources import fetch_metadata, MetadataFetchError

log=logging.getLogger('corpus')

def hash_password(password,salt):
    return hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()

def setup_auth(settings):
    created={}
    with connection(settings) as db:
        r=db.execute('SELECT * FROM credential WHERE id=1').fetchone()
        if settings.password or not r:
            password=settings.password or secrets.token_urlsafe(18)
            salt=secrets.token_hex(16)
            db.execute('INSERT OR REPLACE INTO credential VALUES(1,?,?)',(salt,hash_password(password,salt)))
            if not settings.password: created['password']=password
    tokenfile=settings.data_dir/'ingest-token'
    if not settings.ingest_token:
        if tokenfile.exists(): settings.ingest_token=tokenfile.read_text().strip()
        else:
            settings.ingest_token=secrets.token_urlsafe(32)
            fd=os.open(tokenfile,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as f: f.write(settings.ingest_token+'\n')
    if created:
        path=settings.data_dir/'initial-credentials.json'
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as f: json.dump(created,f)
        log.warning('Initial login password saved to %s (mode 0600).',path)

class Limiter:
    def __init__(self): self.calls=defaultdict(deque)
    def allow(self,key,count,seconds):
        now=time.monotonic(); values=self.calls[key]
        while values and values[0]<now-seconds: values.popleft()
        if len(values)>=count: return False
        values.append(now)
        # Bound bookkeeping under many distinct remote IPs.
        if len(self.calls)>10000:
            for k in list(self.calls):
                if not self.calls[k] or self.calls[k][-1]<now-seconds: del self.calls[k]
        return True

def create_app(settings=None):
    settings=settings or Settings()
    sessions={}; limits=Limiter()
    @asynccontextmanager
    async def lifespan(app):
        initialize(settings); setup_auth(settings)
        stop=asyncio.Event()
        task=asyncio.create_task(worker(settings,stop)) if settings.pdf_jobs else None
        yield
        stop.set()
        if task: await task
    app=FastAPI(title='Corpus',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    app.state.settings=settings
    def authenticated(request):
        if settings.dev: return True
        token=request.cookies.get('corpus_session','')
        expiry=sessions.get(token,0)
        if expiry<time.time():
            sessions.pop(token,None); return False
        return True
    @app.middleware('http')
    async def gate(request,call_next):
        path=request.url.path
        length=request.headers.get('content-length')
        if length:
            try:
                if int(length)>2_000_000: return JSONResponse({'detail':'Request exceeds 2 MB'},status_code=413)
            except ValueError: return JSONResponse({'detail':'Invalid Content-Length'},status_code=400)
        # Bound streaming/chunked bodies too, before FastAPI parses JSON.
        if request.method in ('POST','PUT','PATCH'):
            chunks=[]; size=0
            async for chunk in request.stream():
                size+=len(chunk)
                if size>2_000_000: return JSONResponse({'detail':'Request exceeds 2 MB'},status_code=413)
                chunks.append(chunk)
            request._body=b''.join(chunks)
        public=path in ('/api/session','/api/login','/api/config')
        if path=='/api/ingest':
            supplied=request.headers.get('authorization','')
            if not hmac.compare_digest(supplied,'Bearer '+settings.ingest_token): return JSONResponse({'detail':'A valid ingest Bearer token is required'},status_code=401)
        elif path.startswith('/api/') and not public and not authenticated(request):
            return JSONResponse({'detail':'Sign in to Corpus'},status_code=401)
        if request.method not in ('GET','HEAD','OPTIONS') and path!='/api/ingest':
            origin=request.headers.get('origin')
            if origin and origin.rstrip('/')!=str(request.base_url).rstrip('/'):
                return JSONResponse({'detail':'Cross-origin writes are not allowed'},status_code=403)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['X-Frame-Options']='DENY'
        if path.startswith('/api/'): response.headers['Cache-Control']='no-store'
        return response
    @app.exception_handler(QueryError)
    async def query_error(request,e): return JSONResponse({'detail':{'message':e.message,'position':e.position}},status_code=400)
    @app.exception_handler(ValueError)
    async def value_error(request,e): return JSONResponse({'detail':str(e)},status_code=400)
    @app.exception_handler(sqlite3.Error)
    async def database_error(request,e): return JSONResponse({'detail':str(e)},status_code=400)
    @app.get('/api/config')
    def config():
        with connection(settings,True) as db: fields=[dict(r) for r in db.execute('SELECT * FROM fields ORDER BY rowid')]
        return {'fields':fields,'auth_required':not settings.dev,'sql_local_only':settings.sql_local_only}
    @app.get('/api/session')
    def session(request:Request): return {'authenticated':authenticated(request)}
    @app.post('/api/login')
    def login(request:Request,body:dict):
        ip=request.client.host if request.client else 'unknown'
        if not limits.allow('login:'+ip,10,300): raise HTTPException(429,'Too many login attempts; try again in five minutes')
        password=body.get('password','')
        if not isinstance(password,str) or len(password)>1000: raise HTTPException(400,'Invalid password')
        with connection(settings,True) as db: r=db.execute('SELECT * FROM credential WHERE id=1').fetchone()
        if not hmac.compare_digest(hash_password(password,r['salt']),r['password_hash']): raise HTTPException(401,'Incorrect password')
        token=secrets.token_urlsafe(32)
        for k in list(sessions):
            if sessions[k]<time.time(): del sessions[k]
        sessions[token]=time.time()+86400*7
        response=JSONResponse({'authenticated':True})
        response.set_cookie('corpus_session',token,httponly=True,secure=settings.secure_cookie,samesite='strict',max_age=86400*7)
        return response
    @app.post('/api/logout')
    def logout(request:Request):
        sessions.pop(request.cookies.get('corpus_session',''),None)
        r=JSONResponse({'authenticated':False}); r.delete_cookie('corpus_session'); return r
    @app.get('/api/papers')
    def papers(q:str='',facets:str='{}',limit:int=100,offset:int=0):
        if not 1<=limit<=500 or offset<0: raise HTTPException(400,'Invalid pagination')
        with connection(settings,True) as db: return search(db,q,json.loads(facets),limit,offset)
    @app.get('/api/paper')
    def paper(id:str,q:str=''):
        with connection(settings,True) as db: p=get_paper(db,id,q)
        if p is None: raise HTTPException(404,'Paper not found')
        return p
    @app.post('/api/duplicates')
    def duplicates(body:dict):
        if not isinstance(body.get('title',''),str): raise ValueError('Title must be text')
        with connection(settings,True) as db: return {'candidates':duplicate_candidates(db,body,body.get('id'))}
    @app.post('/api/papers')
    def save_paper(body:dict):
        with connection(settings) as db:
            db.execute('BEGIN IMMEDIATE')
            pid=body.get('id'); old=get_paper(db,pid) if pid else None
            if pid and old is None: raise HTTPException(404,'Paper not found')
            p=validate_paper(db,body,old)
            duplicate=find_duplicate(db,p,settings.dedupe_threshold,pid)
            if duplicate: raise HTTPException(409,{'message':'This paper already exists','duplicate':duplicate})
            run=None
            if not pid:
                run=add_run(db,'manual','Added by hand')
                db.execute('UPDATE ingest_run SET accepted=1 WHERE id=?',(run,))
            pid=write_paper(db,p,run,pid,query='Added by hand')
            return {'paper':get_paper(db,pid)}
    @app.post('/api/metadata')
    def metadata(body:dict):
        if not limits.allow('metadata',30,60): raise HTTPException(429,'Metadata rate limit reached; retry in one minute')
        identifier=body.get('identifier','')
        if not isinstance(identifier,str) or len(identifier)>3000: raise ValueError('Invalid identifier')
        try: return fetch_metadata(identifier)
        except MetadataFetchError as e: raise HTTPException(502,'Metadata source unavailable: '+str(e)[:300])
        except ValueError: raise
        except Exception as e: raise HTTPException(502,'Metadata source unavailable: '+str(e)[:300])
    @app.post('/api/ingest')
    def ingest(body:dict):
        if not limits.allow('ingest',20,60): raise HTTPException(429,'At most 20 ingest batches per minute')
        items=body.get('papers')
        if not isinstance(items,list) or not 1<=len(items)<=100: raise ValueError('papers must contain 1–100 proposals')
        agent=body.get('agent',body.get('agent_identifier','ingest-agent')); query=body.get('query','')
        if not isinstance(agent,str) or not agent.strip() or len(agent)>200 or not isinstance(query,str) or len(query)>4000: raise ValueError('Invalid agent or query')
        with connection(settings) as db:
            db.execute('BEGIN IMMEDIATE')
            run=add_run(db,agent,query,len(items),body.get('extra',{}))
            results=[]; counts={'accepted':0,'duplicate':0,'rejected':0}
            for i,item in enumerate(items):
                db.execute('SAVEPOINT ingest_item')
                try:
                    p=validate_paper(db,item,ingest=True)
                    item_query=item.get('query',query)
                    if not isinstance(item_query,str) or len(item_query)>4000: raise ValueError('Item query must be text under 4,000 characters')
                    dup=find_duplicate(db,p,settings.dedupe_threshold)
                    if dup:
                        db.execute('INSERT INTO provenance(paper_id,run_id,query,status,similarity,reason) VALUES(?,?,?,?,?,?)',(dup['id'],run,item_query,'duplicate',dup['similarity'],dup['reason']))
                        counts['duplicate']+=1
                        results.append({'index':i,'status':'duplicate','id':dup['id'],'reason':dup['reason'],'similarity':dup['similarity']})
                    else:
                        near=duplicate_candidates(db,p)
                        similarity=near[0]['similarity'] if near else 0
                        pid=write_paper(db,p,run,query=item_query,similarity=similarity)
                        counts['accepted']+=1; results.append({'index':i,'status':'accept','id':pid,'reason':'Added as unreviewed; PDF queued'})
                    db.execute('RELEASE ingest_item')
                except (ValueError,sqlite3.IntegrityError,QueryError) as e:
                    db.execute('ROLLBACK TO ingest_item'); db.execute('RELEASE ingest_item')
                    counts['rejected']+=1; results.append({'index':i,'status':'reject','reason':str(e)})
            extra=body.get('extra',{})
            db.execute('UPDATE ingest_run SET accepted=?,duplicate=?,rejected=?,extra=? WHERE id=?',(counts['accepted'],counts['duplicate'],counts['rejected'],json.dumps({'agent_metadata':extra,'results':results},ensure_ascii=False),run))
            return {'run_id':run,'results':results,**counts}
    @app.post('/api/sql')
    def sql(request:Request,body:dict):
        if settings.sql_local_only and (not request.client or request.client.host not in ('127.0.0.1','::1','testclient')): raise HTTPException(403,'SQL is restricted to localhost')
        return readonly_sql(settings,body.get('sql',''))
    @app.get('/api/smart-lists')
    def lists():
        with connection(settings,True) as db:
            return {'lists':[{**dict(r),'facets':json.loads(r['facets'])} for r in db.execute('SELECT * FROM smart_lists ORDER BY name')]}
    @app.post('/api/smart-lists')
    def save_list(request:Request,body:dict):
        name=body.get('name',''); query=body.get('query',''); kind=body.get('kind','query')
        if not isinstance(name,str) or not 1<=len(name.strip())<=100 or kind not in ('query','sql'): raise ValueError('A name (1–100 characters) and valid kind are required')
        if not isinstance(query,str): raise ValueError('Query must be text')
        facets=body.get('facets',{})
        if kind=='query': compile_query(query,facets)
        else: sql(request,{'sql':query})
        with connection(settings) as db:
            db.execute('INSERT INTO smart_lists(name,query,kind,facets) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET query=excluded.query,kind=excluded.kind,facets=excluded.facets',(name.strip(),query,kind,json.dumps(facets if kind=='query' else {})))
        return {'saved':True}
    @app.delete('/api/smart-lists/{id}')
    def delete_list(id:int):
        with connection(settings) as db: db.execute('DELETE FROM smart_lists WHERE id=?',(id,))
        return {'deleted':True}
    @app.get('/api/stats')
    def statistics():
        with connection(settings,True) as db: return stats(db,settings)
    @app.post('/api/export')
    def export(body:dict):
        with connection(settings,True) as db: return export_papers(db,body)
    @app.post('/api/artifacts/{id}/cache')
    @app.post('/api/artifacts/{id}/retry')
    def retry(id:int):
        with connection(settings) as db:
            changed=db.execute("UPDATE artifacts SET fetch_status='pending',error=NULL WHERE id=? AND kind='pdf' AND fetch_status IN ('failed','linked')",(id,)).rowcount
        return {'queued':bool(changed)}
    @app.get('/api/artifacts/{id}/file')
    def file(id:int):
        with connection(settings,True) as db: r=db.execute("SELECT * FROM artifacts WHERE id=? AND fetch_status='cached'",(id,)).fetchone()
        if not r or not r['local_path']: raise HTTPException(404,'PDF is not cached')
        path=Path(r['local_path']).resolve()
        if not path.is_relative_to(settings.cache_dir) or not path.is_file(): raise HTTPException(404,'Cached PDF is missing')
        return FileResponse(path,media_type='application/pdf',filename=path.name,content_disposition_type='inline')
    app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
    @app.get('/')
    def index(): return FileResponse(ROOT/'static'/'index.html',media_type='text/html')
    return app
