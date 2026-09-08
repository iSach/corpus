import csv, io, json
from .db import paper_dict
from .query import compile_query, SCORES

def export_papers(db, options):
    scope=options.get('scope','query'); fmt=options.get('format','bibtex'); include=options.get('include',{})
    if scope not in ('query','all','selection') or fmt not in ('bibtex','json','csv'): raise ValueError('Unknown export scope or format')
    if not isinstance(include,dict): raise ValueError('include must be an object')
    c=compile_query(options.get('q','') if scope=='query' else '',options.get('facets') if scope=='query' else None)
    where,params=c.where,list(c.params)
    if scope=='selection':
        ids=options.get('ids',[])
        if not isinstance(ids,list) or len(ids)>10000 or any(not isinstance(x,str) for x in ids): raise ValueError('ids must be a list of at most 10,000 paper IDs')
        where='p.id IN ('+','.join('?' for _ in ids)+')' if ids else '0'; params=ids
    papers=[paper_dict(r) for r in db.execute('SELECT p.* FROM papers p WHERE '+where+' ORDER BY '+c.order,params)]
    byid={p['id']:p for p in papers}
    for p in papers: p['tags']=[]; p['artifacts']=[]
    for r in db.execute('SELECT pt.paper_id,t.name FROM paper_tags pt JOIN tags t ON t.id=pt.tag_id ORDER BY t.name'):
        if r['paper_id'] in byid: byid[r['paper_id']]['tags'].append(r['name'])
    for r in db.execute('SELECT * FROM artifacts ORDER BY id'):
        if r['paper_id'] in byid:
            a=dict(r)
            if a['kind'] in ('code','data') and not include.get('code',False): continue
            if not include.get('local_paths',False): a.pop('local_path',None)
            byid[r['paper_id']]['artifacts'].append(a)
    for p in papers:
        if not include.get('scores',True):
            for s in SCORES: p.pop(s,None)
        if not include.get('notes',True): p.pop('notes',None)
    if fmt=='json': text=json.dumps(papers,ensure_ascii=False,indent=2); ext='json'; mime='application/json'
    elif fmt=='csv':
        columns=['id','title','authors','venue','year','field','state','canonical_url','arxiv_id','doi','tags']
        if include.get('scores',True): columns+=list(SCORES)
        if include.get('notes',True): columns+=['notes']
        columns+=['artifacts']
        out=io.StringIO(); w=csv.DictWriter(out,fieldnames=columns,extrasaction='ignore'); w.writeheader()
        for p in papers:
            row={k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in p.items()}
            # Preserve content but prevent formula execution in spreadsheet apps.
            for k,v in row.items():
                if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@','\t','\r')): row[k]="'"+v
            w.writerow(row)
        text=out.getvalue(); ext='csv'; mime='text/csv'
    else:
        def tex(value):
            mapping={'\\':r'\textbackslash{}','{':r'\{','}':r'\}','&':r'\&','%':r'\%','$':r'\$','#':r'\#','_':r'\_','~':r'\textasciitilde{}','^':r'\textasciicircum{}'}
            return ''.join(mapping.get(c,c) for c in str(value)).replace('\n',' ')
        entries=[]
        for p in papers:
            data={'title':p['title'],'author':' and '.join(p['authors']),'year':p['year'],'url':p['canonical_url'],'doi':p['doi'],'eprint':p['arxiv_id']}
            kind='inproceedings' if p['venue_class']=='conference' else 'article'
            data['booktitle' if kind=='inproceedings' else 'journal']=p['venue']
            keywords=p['tags']+['field='+p['field']]
            if include.get('scores',True): keywords += [s+'='+str(p[s]) for s in SCORES if p.get(s) is not None]
            data['keywords']=', '.join(keywords)
            if include.get('notes',True): data['annote']=p.get('notes','')
            if include.get('code',False): data['howpublished']='; '.join(a['kind']+': '+a['url'] for a in p['artifacts'] if a['kind'] in ('code','data'))
            if include.get('local_paths',False): data['file']='; '.join(a['local_path'] for a in p['artifacts'] if a.get('local_path'))
            entries.append('@'+kind+'{'+p['bibtex_key']+',\n'+',\n'.join('  '+k.ljust(10)+' = {'+tex(v)+'}' for k,v in data.items() if v is not None and v!='')+'\n}')
        text='\n\n'.join(entries); ext='bib'; mime='application/x-bibtex'
    return {'text':text,'filename':'corpus.'+ext,'mime':mime,'count':len(papers)}
