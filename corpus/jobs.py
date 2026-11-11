"""One durable queue, serviced by a thread in the application process."""
import asyncio, hashlib, io, logging, os
from .db import connection
from .review import download_allowed, finish_preparation

log=logging.getLogger('corpus.pdf')

def fetch_one(settings,artifact):
    from .sources import safe_download
    from pypdf import PdfReader
    aid,paper_id=artifact['id'],artifact['paper_id']
    target=settings.cache_dir/(hashlib.sha256(artifact['url'].encode()).hexdigest()+'.pdf')
    temp=target.with_suffix('.part')
    try:
        with connection(settings) as db:
            allowed=db.execute("SELECT 1 FROM artifacts a JOIN papers p ON p.id=a.paper_id WHERE a.id=? AND p.admission='approved'",(aid,)).fetchone()
            if not allowed and not download_allowed(db,aid):
                db.execute("UPDATE artifacts SET fetch_status='linked' WHERE id=? AND fetch_status IN ('pending','fetching')",(aid,))
                return
        # Reclaim files from artifacts removed in Add/Edit. The worker is serial,
        # so no other downloader can be between publishing and recording a file.
        with connection(settings,True) as db:
            referenced={r[0] for r in db.execute('SELECT local_path FROM artifacts WHERE local_path IS NOT NULL')}
        for cached in settings.cache_dir.glob('*.pdf'):
            if str(cached) not in referenced: cached.unlink(missing_ok=True)
        used=sum(p.stat().st_size for p in settings.cache_dir.glob('*.pdf'))
        if used>=settings.cache_max_bytes: raise ValueError('PDF cache quota reached; increase CORPUS_CACHE_MAX_BYTES or remove cached artifacts')
        data=safe_download(artifact['url'],max_bytes=min(settings.pdf_max_bytes,settings.cache_max_bytes-used))
        if not data.lstrip().startswith(b'%PDF-'): raise ValueError('Source returned a non-PDF response')
        reader=PdfReader(io.BytesIO(data))
        if len(reader.pages)>1000: raise ValueError('PDF exceeds the 1,000-page limit')
        pages=[]; total=0
        for n,page in enumerate(reader.pages,1):
            text=(page.extract_text() or '').replace('\x00','').replace('\x01','').replace('\x02','')
            total+=len(text)
            if total>20_000_000: raise ValueError('Extracted PDF text exceeds 20 million characters')
            pages.append((paper_id,aid,n,text))
        temp.write_bytes(data); os.replace(temp,target)
        with connection(settings) as db:
            exists=db.execute('SELECT 1 FROM artifacts WHERE id=? AND url=?',(aid,artifact['url'])).fetchone()
            approved=db.execute("SELECT 1 FROM papers WHERE id=? AND admission='approved'",(paper_id,)).fetchone()
            if not exists or (not approved and not download_allowed(db,aid)): return
            db.execute('DELETE FROM pdf_text WHERE artifact_id=?',(aid,))
            db.executemany('INSERT INTO pdf_text(paper_id,artifact_id,page,text) VALUES(?,?,?,?)',pages)
            db.execute("UPDATE artifacts SET local_path=?,fetch_status='cached',fetched_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),error=NULL WHERE id=?",(str(target),aid))
            finish_preparation(db,aid)
    except Exception as e:
        log.warning('PDF fetch failed for artifact %s: %s',aid,e)
        with connection(settings) as db:
            db.execute("UPDATE artifacts SET fetch_status='failed',fetched_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),error=? WHERE id=?",(str(e)[:1000],aid))
            finish_preparation(db,aid,str(e))
    finally:
        temp.unlink(missing_ok=True)

def next_artifact(settings):
    with connection(settings) as db:
        db.execute('BEGIN IMMEDIATE')
        rows=db.execute("SELECT a.*,p.admission FROM artifacts a JOIN papers p ON p.id=a.paper_id WHERE a.kind='pdf' AND a.fetch_status='pending' AND p.admission!='dismissed' ORDER BY EXISTS (SELECT 1 FROM full_text_checks c WHERE c.artifact_id=a.id AND c.status='queued') DESC,a.id").fetchall()
        for row in rows:
            if row['admission']=='approved' or download_allowed(db,row['id']):
                db.execute("UPDATE artifacts SET fetch_status='fetching',error=NULL WHERE id=?",(row['id'],))
                db.execute("UPDATE full_text_checks SET status='preparing',updated=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE artifact_id=? AND status='queued'",(row['id'],))
                return dict(row)
            db.execute("UPDATE artifacts SET fetch_status='linked' WHERE id=?",(row['id'],))
        return None

async def worker(settings, stop):
    with connection(settings) as db:
        db.execute("UPDATE artifacts SET fetch_status='pending' WHERE fetch_status='fetching'")
        db.execute("UPDATE full_text_checks SET status='queued' WHERE status='preparing'")
    while not stop.is_set():
        artifact=await asyncio.to_thread(next_artifact,settings)
        if artifact:
            await asyncio.to_thread(fetch_one,settings,artifact)
        else:
            try: await asyncio.wait_for(stop.wait(),timeout=2)
            except asyncio.TimeoutError: pass
