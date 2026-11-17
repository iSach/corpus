import csv, io, json, re, unicodedata
from .db import paper_dict, normalized_text
from .query import compile_query, SCORES

# Server defaults reproduce the original export, so API callers that send no
# options keep getting the same text. The app sends explicit options.
INCLUDE_DEFAULTS = {'scores':True,'notes':True,'code':False,'local_paths':False,'doi':True,'eprint':True,'keywords':True}
BIBTEX_CHOICES = {
    'url': ('canonical','none','arxiv','published','best'),
    'keys': ('stored','short'),
    'venue': ('full','short'),
    'protect': ('none','smart','all'),
    'accents': ('unicode','latex'),
    'preprints': ('article','misc'),
}
BIBTEX_DEFAULTS = {'url':'canonical','keys':'stored','venue':'full','protect':'none','accents':'unicode','preprints':'article','authors':0}
SORTS = ('query','added','year','author','key','title')

# Common machine-learning venues. Workshops keep their full name: a workshop
# at NeurIPS is not NeurIPS.
SHORT_VENUES = [
    (r'neural information processing systems|\bneurips\b|\bnips\b', 'NeurIPS'),
    (r'international conference on machine learning|\bicml\b', 'ICML'),
    (r'international conference on learning representations|\biclr\b', 'ICLR'),
    (r'computer vision and pattern recognition|\bcvpr\b', 'CVPR'),
    (r'international conference on computer vision|\biccv\b', 'ICCV'),
    (r'european conference on computer vision|\beccv\b', 'ECCV'),
    (r'artificial intelligence and statistics|\baistats\b', 'AISTATS'),
    (r'uncertainty in artificial intelligence|\buai\b', 'UAI'),
    (r'conference on learning theory|\bcolt\b', 'COLT'),
    (r'conference on robot learning|\bcorl\b', 'CoRL'),
    (r'learning for dynamics and control|\bl4dc\b', 'L4DC'),
    (r'international joint conference on artificial intelligence|\bijcai\b', 'IJCAI'),
    (r'\baaai\b', 'AAAI'),
    (r'empirical methods in natural language processing|\bemnlp\b', 'EMNLP'),
    (r'transactions on machine learning research|\btmlr\b', 'TMLR'),
    (r'journal of machine learning research|\bjmlr\b', 'JMLR'),
]
KEY_STOPWORDS = {'a','an','the','on','of','for','in','to','and','with','towards','toward','via','from','is','are','do','does','at','by','as','into','what','how','why','when'}
ACCENTS = {'\u0301':"'",'\u0300':'`','\u0302':'^','\u0308':'"','\u0303':'~','\u0327':'c','\u030c':'v','\u030a':'r','\u0304':'=','\u0306':'u','\u0307':'.','\u0328':'k','\u030b':'H'}
LETTERS = {'ß':r'{\ss}','ø':r'{\o}','Ø':r'{\O}','æ':r'{\ae}','Æ':r'{\AE}','œ':r'{\oe}','Œ':r'{\OE}','ł':r'{\l}','Ł':r'{\L}','ı':r'{\i}','đ':r'{\dj}','Đ':r'{\DJ}'}


def _choice(value, allowed, name):
    if value not in allowed: raise ValueError(f'Unknown export option for {name}')
    return value


def export_options(options):
    include=options.get('include',{}); bibtex=options.get('bibtex',{})
    if not isinstance(include,dict) or not isinstance(bibtex,dict): raise ValueError('include and bibtex must be objects')
    inc={k:bool(include.get(k,v)) for k,v in INCLUDE_DEFAULTS.items()}
    bib={**BIBTEX_DEFAULTS}
    for k,allowed in BIBTEX_CHOICES.items(): bib[k]=_choice(bibtex.get(k,bib[k]),allowed,k)
    authors=bibtex.get('authors',0)
    if not isinstance(authors,int) or isinstance(authors,bool) or not 0<=authors<=100: raise ValueError('authors must be 0 (all) or a maximum between 1 and 100')
    bib['authors']=authors
    return inc,bib,_choice(options.get('sort','query'),SORTS,'sort')


def surname(name):
    name=str(name or '').strip()
    last=name.split(',')[0] if ',' in name else (name.split() or ['anon'])[-1]
    return ''.join(c for c in unicodedata.normalize('NFKD',last) if c.isascii() and c.isalnum()).lower() or 'anon'


def short_keys(db):
    """authorYEARword keys, stable for the whole library so a paper's key does
    not depend on which other papers are in this export."""
    keys,used={},set()
    for r in db.execute("SELECT id,authors,year,title FROM papers WHERE admission='approved' ORDER BY added,id"):
        authors=json.loads(r['authors'] or '[]')
        words=[w for w in normalized_text(r['title'] or '').split() if w not in KEY_STOPWORDS and w.isascii()]
        base=surname(authors[0] if authors else 'anon')+str(r['year'] or 'nd')+(words[0] if words else '')
        key,suffix=base,ord('b')
        while key in used: key=base+chr(suffix); suffix+=1
        used.add(key); keys[r['id']]=key
    return keys


def short_venue(venue):
    if not venue or 'workshop' in venue.lower(): return venue
    for pattern,name in SHORT_VENUES:
        if re.search(pattern,venue,re.I): return name
    return venue


def tex(value):
    mapping={'\\':r'\textbackslash{}','{':r'\{','}':r'\}','&':r'\&','%':r'\%','$':r'\$','#':r'\#','_':r'\_','~':r'\textasciitilde{}','^':r'\textasciicircum{}'}
    return ''.join(mapping.get(c,c) for c in str(value)).replace('\n',' ')


def latex_accents(text):
    out=[]
    for c in text:
        if c.isascii(): out.append(c); continue
        if c in LETTERS: out.append(LETTERS[c]); continue
        parts=unicodedata.normalize('NFD',c)
        base,marks=parts[0],parts[1:]
        if base.isascii() and marks and all(m in ACCENTS for m in marks):
            for m in marks: base='\\'+ACCENTS[m]+('{'+base+'}' if ACCENTS[m].isalpha() else base)
            out.append('{'+base+'}')
        else: out.append(c)
    return ''.join(out)


def protect_title(title, mode):
    if mode=='all': return '{'+tex(title)+'}'
    if mode=='none': return tex(title)
    # Brace acronyms and mixed-case names (PDE, ResNet, 3D) so title-casing
    # bibliography styles keep their capitals; ordinary words stay styleable.
    def special(part, hyphenated):
        letters=[c for c in part if c.isalpha()]
        upper=sum(c.isupper() for c in letters)
        return upper>=2 or (upper and not letters[0].isupper()) or (upper and part[:1].isdigit()) or (hyphenated and len(letters)==1 and upper==1)
    def word(token):
        parts=token.split('-')
        return '{'+tex(token)+'}' if any(special(part,len(parts)>1) for part in parts) else tex(token)
    return ''.join(word(t) if t.strip() else t for t in re.split(r'(\s+)',title))


def is_arxiv_doi(doi): return bool(doi) and doi.lower().startswith('10.48550/')


def entry_url(p, mode):
    arxiv='https://arxiv.org/abs/'+p['arxiv_id'] if p['arxiv_id'] else None
    canonical=p['canonical_url'] or None
    published=('https://doi.org/'+p['doi']) if p['doi'] and not is_arxiv_doi(p['doi']) else canonical if canonical and 'arxiv.org' not in canonical.lower() else None
    return {'none':None,'canonical':canonical,'arxiv':arxiv,'published':published,'best':published or arxiv or canonical}[mode]


def bibtex_entry(p, key, inc, bib):
    preprint=p['venue_class']=='preprint' or not p['venue']
    authors=list(p['authors'])
    if bib['authors'] and len(authors)>bib['authors']: authors=authors[:bib['authors']]+['others']
    data={'title':None,'author':' and '.join(authors),'year':p['year']}
    if preprint and p['arxiv_id']:
        # Cite as Google Scholar does; @misc suits styles that print howpublished.
        kind='misc' if bib['preprints']=='misc' else 'article'
        data['howpublished' if kind=='misc' else 'journal']='arXiv preprint arXiv:'+p['arxiv_id']
    else:
        # Cite the published version when there is one; the eprint still points at arXiv.
        kind='inproceedings' if p['venue_class'] in ('conference','other') else 'article'
        data['booktitle' if kind=='inproceedings' else 'journal']=short_venue(p['venue']) if bib['venue']=='short' else p['venue']
    data['url']=entry_url(p,bib['url'])
    if inc['doi']: data['doi']=p['doi']
    if inc['eprint'] and p['arxiv_id']: data.update(eprint=p['arxiv_id'],archiveprefix='arXiv')
    keywords=[]
    if inc['keywords']: keywords+=p['tags']+['topic='+project for project in p['projects']]
    if inc['scores']: keywords+=[s+'='+str(p[s]) for s in SCORES if p.get(s) is not None]
    data['keywords']=', '.join(keywords)
    if inc['notes']: data['annote']=p.get('notes','')
    if inc['code']:
        code='; '.join(a['kind']+': '+a['url'] for a in p['artifacts'] if a['kind'] in ('code','data'))
        data['howpublished']='; '.join(v for v in (data.get('howpublished'),code) if v)
    if inc['local_paths']: data['file']='; '.join(a['local_path'] for a in p['artifacts'] if a.get('local_path'))
    fields={k:(protect_title(p['title'],bib['protect']) if k=='title' else tex(v)) for k,v in data.items() if k=='title' or (v is not None and v!='')}
    if bib['accents']=='latex': fields={k:latex_accents(v) for k,v in fields.items()}
    return '@'+kind+'{'+key+',\n'+',\n'.join('  '+k.ljust(10)+' = {'+v+'}' for k,v in fields.items())+'\n}'


def export_papers(db, options):
    scope=options.get('scope','query'); fmt=options.get('format','bibtex')
    if scope not in ('query','all','selection') or fmt not in ('bibtex','json','csv'): raise ValueError('Unknown export scope or format')
    include,bib,sort=export_options(options)
    c=compile_query(options.get('q','') if scope=='query' else '',options.get('facets') if scope=='query' else None)
    where,params=c.where,list(c.params)
    if scope=='selection':
        ids=options.get('ids',[])
        if not isinstance(ids,list) or len(ids)>10000 or any(not isinstance(x,str) for x in ids): raise ValueError('ids must be a list of at most 10,000 paper IDs')
        where='p.id IN (SELECT value FROM json_each(?))' if ids else '0'; params=[json.dumps(ids)] if ids else []
    papers=[paper_dict(r) for r in db.execute("SELECT p.* FROM papers p WHERE p.admission='approved' AND ("+where+') ORDER BY '+c.order,params)]
    byid={p['id']:p for p in papers}
    for p in papers: p['tags']=[]; p['artifacts']=[]; p['projects']=[]
    for row in db.execute('SELECT paper_id,field_id FROM paper_projects ORDER BY field_id'):
        if row['paper_id'] in byid: byid[row['paper_id']]['projects'].append(row['field_id'])
    for r in db.execute('SELECT pt.paper_id,t.name FROM paper_tags pt JOIN tags t ON t.id=pt.tag_id ORDER BY t.name'):
        if r['paper_id'] in byid: byid[r['paper_id']]['tags'].append(r['name'])
    for r in db.execute('SELECT * FROM artifacts ORDER BY id'):
        if r['paper_id'] in byid:
            a=dict(r)
            if a['kind'] in ('code','data') and not include['code']: continue
            if not include['local_paths']: a.pop('local_path',None)
            byid[r['paper_id']]['artifacts'].append(a)
    keys=short_keys(db) if fmt=='bibtex' and bib['keys']=='short' else {}
    for p in papers: p['cite_key']=keys.get(p['id'],p['bibtex_key'])
    if sort=='added': papers.sort(key=lambda p:p['added'] or '',reverse=True)
    elif sort=='year': papers.sort(key=lambda p:(-(p['year'] or 0),surname(p['authors'][0] if p['authors'] else ''),p['title'].casefold()))
    elif sort=='author': papers.sort(key=lambda p:(surname(p['authors'][0] if p['authors'] else ''),p['year'] or 0,p['title'].casefold()))
    elif sort=='key': papers.sort(key=lambda p:p['cite_key'])
    elif sort=='title': papers.sort(key=lambda p:p['title'].casefold())
    for p in papers:
        if not include['scores']:
            for s in SCORES: p.pop(s,None)
        if not include['notes']: p.pop('notes',None)
    if fmt=='json':
        for p in papers: p.pop('cite_key',None)
        text=json.dumps(papers,ensure_ascii=False,indent=2); ext='json'; mime='application/json'
    elif fmt=='csv':
        columns=['id','title','authors','venue','year','topic','topics','state','canonical_url','arxiv_id','doi','tags']
        if include['scores']: columns+=list(SCORES)
        if include['notes']: columns+=['notes']
        columns+=['artifacts']
        out=io.StringIO(); w=csv.DictWriter(out,fieldnames=columns,extrasaction='ignore'); w.writeheader()
        for p in papers:
            row={k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in {**p,'topic':p['field'],'topics':p['projects']}.items()}
            # Preserve content but prevent formula execution in spreadsheet apps.
            for k,v in row.items():
                if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@','\t','\r')): row[k]="'"+v
            w.writerow(row)
        text=out.getvalue(); ext='csv'; mime='text/csv'
    else:
        text='\n\n'.join(bibtex_entry(p,p['cite_key'],include,bib) for p in papers); ext='bib'; mime='application/x-bibtex'
    return {'text':text,'filename':'corpus.'+ext,'mime':mime,'count':len(papers)}


# The Export page remembers its settings and which papers are left out, per
# library, so they follow the owner across devices.
def get_preferences(db):
    row=db.execute("SELECT value FROM preferences WHERE key='export'").fetchone()
    excluded=[r[0] for r in db.execute('SELECT paper_id FROM export_exclusions ORDER BY paper_id')]
    return {'settings':json.loads(row[0]) if row else None,'excluded':excluded}


def save_preferences(db, settings):
    if not isinstance(settings,dict): raise ValueError('settings must be an object')
    text=json.dumps(settings,ensure_ascii=False,separators=(',',':'))
    if len(text)>20000: raise ValueError('Export settings are too large')
    db.execute("INSERT INTO preferences(key,value) VALUES('export',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(text,))
    return {'ok':True}


def set_exclusions(db, ids, excluded):
    if not isinstance(ids,list) or not 1<=len(ids)<=10000 or any(not isinstance(x,str) for x in ids) or not isinstance(excluded,bool):
        raise ValueError('Send 1–10,000 paper IDs and whether to exclude them')
    if excluded:
        db.execute('INSERT OR IGNORE INTO export_exclusions(paper_id) SELECT id FROM papers WHERE id IN (SELECT value FROM json_each(?))',(json.dumps(ids),))
    else:
        db.execute('DELETE FROM export_exclusions WHERE paper_id IN (SELECT value FROM json_each(?))',(json.dumps(ids),))
    return {'excluded':[r[0] for r in db.execute('SELECT paper_id FROM export_exclusions ORDER BY paper_id')]}


def export_papers_list(db):
    """Every library paper with its topics, for the Export page's paper list."""
    rows=db.execute("SELECT p.id,p.title,p.authors,p.venue,p.year,p.added,(SELECT json_group_array(field_id) FROM paper_projects pp WHERE pp.paper_id=p.id) topics FROM papers p WHERE p.admission='approved' ORDER BY p.added DESC,p.id").fetchall()
    return [{**dict(r),'authors':json.loads(r['authors'] or '[]'),'topics':json.loads(r['topics'] or '[]')} for r in rows]
