from .query import SCORES

def stats(db,settings):
    # Aggregated in SQLite; no paper-by-paper computation in the browser.
    summary=dict(db.execute("""SELECT count(*) total,
      coalesce(sum(state='unreviewed'),0) unreviewed,coalesce(sum(state='reviewed'),0) reviewed,coalesce(sum(state='archived'),0) archived,
      coalesce(sum(rel>=8),0) shortlist,coalesce(sum(reimpl>=8),0) reimplementation,
      (SELECT count(DISTINCT paper_id) FROM artifacts WHERE kind='code') code,
      (SELECT count(DISTINCT paper_id) FROM pdf_text WHERE length(text)>0) "indexed"
      FROM papers""").fetchone())
    summary['states']={k:summary.pop(k) for k in ('unreviewed','reviewed','archived')}
    summary['fields']=[dict(r) for r in db.execute('SELECT f.id field,f.label,count(p.id) count FROM fields f LEFT JOIN papers p ON p.field=f.id GROUP BY f.id ORDER BY f.id')]
    summary['distributions']={}
    for key in SCORES:
        bins=[0]*11
        for r in db.execute(f'SELECT {key},count(*) FROM papers WHERE {key} IS NOT NULL GROUP BY {key}'): bins[r[0]]=r[1]
        mean=sum(i*n for i,n in enumerate(bins))/sum(bins) if sum(bins) else None
        summary['distributions'][key]={'bins':bins,'mean':round(mean,2) if mean is not None else None}
    summary['runs']=[dict(r) for r in db.execute('SELECT * FROM ingest_run ORDER BY id DESC LIMIT 14')][::-1]
    accepted,proposed=db.execute('SELECT coalesce(sum(accepted),0),coalesce(sum(proposed),0) FROM ingest_run').fetchone()
    summary.update(accepted=accepted,proposed=proposed,keep_rate=round(accepted/proposed*100,1) if proposed else 0)
    summary['pdf_failures']=[dict(r) for r in db.execute("SELECT a.id artifact_id,a.paper_id,p.title,a.url,a.error FROM artifacts a JOIN papers p ON p.id=a.paper_id WHERE a.fetch_status='failed' ORDER BY a.id DESC LIMIT 100")]
    summary['pdf_pending']=db.execute("SELECT count(*) FROM artifacts WHERE fetch_status IN ('pending','fetching')").fetchone()[0]
    summary['cache_bytes']=sum(p.stat().st_size for p in settings.cache_dir.glob('*.pdf'))
    return summary
