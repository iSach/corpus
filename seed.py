#!/usr/bin/env python3
"""Idempotently seed verified bibliographic records. Personal scores stay unset."""
import argparse, json
from corpus.config import Settings, ROOT
from corpus.db import initialize, connection, add_run, validate_paper, find_duplicate, write_paper

def seed(settings,cache_pdfs=False):
    initialize(settings)
    papers=json.loads((ROOT/'seed_papers.json').read_text())
    if isinstance(papers,dict): papers=papers['papers']
    accepted=duplicate=0
    with connection(settings) as db:
        db.execute('BEGIN IMMEDIATE')
        run=add_run(db,'seed','Verified foundational and related papers; see SEED_SOURCES.md',len(papers))
        for incoming in papers:
            p=validate_paper(db,incoming,ingest=True)
            dup=find_duplicate(db,p,settings.dedupe_threshold)
            if dup:
                duplicate+=1
                db.execute('INSERT INTO provenance(paper_id,run_id,query,status,similarity,reason) VALUES(?,?,?,?,?,?)',(dup['id'],run,'Verified seed bibliography','duplicate',dup['similarity'],dup['reason']))
                if cache_pdfs: db.execute("UPDATE artifacts SET fetch_status='pending' WHERE paper_id=? AND kind='pdf' AND fetch_status IN ('linked','failed')",(dup['id'],))
                continue
            pid=write_paper(db,p,run,query='Verified seed bibliography')
            if not cache_pdfs: db.execute("UPDATE artifacts SET fetch_status='linked' WHERE paper_id=? AND kind='pdf'",(pid,))
            accepted+=1
        db.execute('UPDATE ingest_run SET accepted=?,duplicate=? WHERE id=?',(accepted,duplicate,run))
    return {'accepted':accepted,'duplicate':duplicate,'run_id':run}

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--cache-pdfs',action='store_true',help='Queue PDFs for download when the server starts')
    print(json.dumps(seed(Settings(),p.parse_args().cache_pdfs)))
