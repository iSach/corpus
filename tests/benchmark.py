"""Reproducible synthetic 10k-record search benchmark; writes only a temp directory."""
from tempfile import TemporaryDirectory
from time import perf_counter
from statistics import median
from corpus.config import Settings
from fastapi.testclient import TestClient
from corpus.app import create_app
from corpus.db import initialize,connection,add_run,write_paper,validate_paper,search

def main():
 with TemporaryDirectory(prefix='corpus-benchmark-') as temp:
  settings=Settings(data_dir=temp,pdf_jobs=False,dev=True)
  initialize(settings)
  with connection(settings) as db:
   run=add_run(db,'synthetic-benchmark',proposed=10000)
   for i in range(10000):
    p=validate_paper(db,{'title':f'{"Diffusion composition" if i%2 else "Posterior inference"} scientific experiment {i}', 'authors':[f'Author {i%100}'],'field':'sbi-pretrain' if i%2 else 'diff-compose','rel':i%11,'cred':i%9,'year':2020+i%7,'tags':['science','local-models'],'notes':'A frozen scientific foundation model','artifacts':[{'kind':'pdf','url':f'https://example.org/{i}.pdf'}]})
    pid=write_paper(db,p,run)
    aid=db.execute('SELECT id FROM artifacts WHERE paper_id=?',(pid,)).fetchone()[0]
    db.execute('INSERT INTO pdf_text(paper_id,artifact_id,page,text) VALUES(?,?,?,?)',(pid,aid,4,'Here we discuss posterior collapse and local diffusion priors. '*20))
  cases=[('',{}),('diffusion',{}),('field:sbi-pretrain rel:>7 sort:rel',{}),('pdf:"posterior collapse"',{}),('(diffusion OR posterior) NOT year:<2023',{'rel':7}),('NOT diffusion',{}),('science',{'field':['sbi-pretrain'],'has':['pdf']})]
  with connection(settings,True) as db:
   for q,f in cases:
    timings=[]
    for _ in range(5):
     start=perf_counter(); result=search(db,q,f); timings.append((perf_counter()-start)*1000)
    print(f'DB {q or "[all]":60s} rows={result["total"]:5d} median={median(timings):7.2f}ms max={max(timings):7.2f}ms')
  import json
  with TestClient(create_app(settings)) as client:
   for q,f in cases:
    timings=[]
    for _ in range(5):
     start=perf_counter(); response=client.get('/api/papers',params={'q':q,'facets':json.dumps(f)}); timings.append((perf_counter()-start)*1000)
     assert response.status_code==200,response.text
    print(f'API {q or "[all]":59s} median={median(timings):7.2f}ms max={max(timings):7.2f}ms')
if __name__=='__main__': main()
