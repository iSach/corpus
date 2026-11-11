"""Topic-specific evidence history. Preparing a PDF never implies a completed check.

The owner or discovery agent supplies scientific judgments. Legacy scores and
search queries are retained as suggestions, never promoted to verified evidence.
"""
import hashlib
import json
import re

from fastapi import HTTPException, Request

from .db import connection

PRIORITIES = ('high', 'medium', 'low', 'exclude', 'unknown')
ROLES = ('model', 'evaluation', 'supporting', 'adjacent', 'unknown')
CONFIDENCES = ('high', 'moderate', 'low', 'unknown')


def input_hash(snapshot):
    """Digest of what an assessment is judged against: the paper source and the
    topic's relevance criteria. The snapshot also records the topic name and
    description, which can be edited without invalidating assessments."""
    judged = {key:snapshot[key] for key in ('title', 'abstract', 'source_url', 'pdfs')}
    judged['topic'] = {key:snapshot['topic'][key] for key in ('id', 'relevance_criteria')}
    return hashlib.sha256(json.dumps(judged, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def topic(db, field):
    return dict(db.execute('SELECT id,label,description,relevance_criteria FROM fields WHERE id=?', (field,)).fetchone())


def subject(db, paper_id):
    if not isinstance(paper_id, str):
        raise ValueError('Choose a paper')
    paper = db.execute('SELECT * FROM papers WHERE id=?', (paper_id,)).fetchone()
    if not paper:
        raise HTTPException(404, 'Paper not found')
    pdfs = [dict(r) for r in db.execute("SELECT id,url FROM artifacts WHERE paper_id=? AND kind='pdf' ORDER BY id", (paper_id,))]
    snapshot = {'title':paper['title'], 'abstract':paper['abstract'], 'source_url':paper['canonical_url'],
                'pdfs':pdfs, 'topic':topic(db, paper['field'])}
    return dict(paper), snapshot, input_hash(snapshot), pdfs


def decode(row):
    result = dict(row)
    for key in ('questions', 'sections', 'evidence', 'snapshot'):
        result[key] = json.loads(result[key])
    return result


def current(db, paper_id):
    paper, _, digest, _ = subject(db, paper_id)
    row = db.execute('SELECT * FROM review_assessments WHERE paper_id=? AND field=? AND input_hash=? ORDER BY id DESC LIMIT 1',
                     (paper_id, paper['field'], digest)).fetchone()
    return decode(row) if row else None


def source_version(url):
    match = re.search(r'v\d+(?=(?:\.pdf)?(?:[?#]|$))', url)
    return match[0] if match else ''


def ensure_abstract(db, paper_id):
    paper, snapshot, digest, _ = subject(db, paper_id)
    row = db.execute("SELECT * FROM review_assessments WHERE paper_id=? AND field=? AND input_hash=? AND stage='abstract' ORDER BY id DESC LIMIT 1",
                     (paper_id, paper['field'], digest)).fetchone()
    if row:
        return decode(row)
    aid = db.execute(
        'INSERT INTO review_assessments(paper_id,field,stage,priority,role,confidence,explanation,limitation,source_url,source_version,assessor,input_hash,snapshot) '
        "VALUES(?,?,'abstract','unknown','unknown','unknown',?,?,?,?,?,?,?)",
        (paper_id, paper['field'], 'No relevance assessment has been supplied yet.',
         'Review the abstract against the topic criteria before relying on the suggested match.',
         paper['canonical_url'], source_version(paper['canonical_url']), 'not-assessed', digest, json.dumps(snapshot, ensure_ascii=False)),
    ).lastrowid
    return decode(db.execute('SELECT * FROM review_assessments WHERE id=?', (aid,)).fetchone())


def summary(db, paper_id):
    assessment = current(db, paper_id)
    history = [decode(row) for row in db.execute('SELECT * FROM review_assessments WHERE paper_id=? ORDER BY id DESC', (paper_id,))]
    for entry in history:
        entry.pop('snapshot', None)
    # The live topic brief: a snapshot keeps the name and description its assessment saw.
    live = topic(db, db.execute('SELECT field FROM papers WHERE id=?', (paper_id,)).fetchone()[0])
    if not assessment:
        return {'current':None, 'abstract':None, 'check':None, 'history':history, 'topic':live}
    abstract = assessment if assessment['stage']=='abstract' else decode(db.execute('SELECT * FROM review_assessments WHERE id=?', (assessment['compared_to'],)).fetchone())
    check = db.execute('SELECT * FROM full_text_checks WHERE abstract_id=?', (abstract['id'],)).fetchone()
    return {'current':assessment, 'abstract':abstract, 'check':dict(check) if check else None, 'history':history, 'topic':live}


def request_check(db, paper_id, reason='Requested by you'):
    paper, _, _, pdfs = subject(db, paper_id)
    if paper['admission']=='dismissed':
        raise HTTPException(409, 'Restore this paper before requesting a check')
    abstract = ensure_abstract(db, paper_id)
    old = db.execute('SELECT * FROM full_text_checks WHERE abstract_id=?', (abstract['id'],)).fetchone()
    if old and old['status']!='unavailable':
        # An approval undo or a restart may have paused the artifact queue.
        if old['status'] in ('queued','preparing'):
            db.execute("UPDATE artifacts SET fetch_status='pending' WHERE id=? AND fetch_status='linked'", (old['artifact_id'],))
        return dict(old)
    pdf = pdfs[0] if pdfs else None
    status, error = ('queued', '') if pdf else ('unavailable', 'No PDF is linked. Add a PDF in Edit details, then retry.')
    if pdf:
        inferred = source_version(pdf['url'])
        if re.fullmatch(r'v\d+', abstract['source_version']) and inferred!=abstract['source_version']:
            status, error = 'unavailable', 'Link a PDF of '+abstract['source_version']+' so both assessments use the same version.'
        else:
            artifact = db.execute('SELECT fetch_status FROM artifacts WHERE id=?', (pdf['id'],)).fetchone()
            if artifact['fetch_status']=='cached':
                has_text = db.execute("SELECT 1 FROM pdf_text WHERE artifact_id=? AND length(trim(text))>0 LIMIT 1", (pdf['id'],)).fetchone()
                status, error = ('ready', '') if has_text else ('unavailable', 'This PDF has no extractable text. Open the source to inspect it.')
            else:
                db.execute("UPDATE artifacts SET fetch_status='pending',error=NULL WHERE id=? AND fetch_status IN ('linked','failed')", (pdf['id'],))
    cid = db.execute(
        'INSERT INTO full_text_checks(paper_id,abstract_id,artifact_id,status,reason,error) VALUES(?,?,?,?,?,?) '
        "ON CONFLICT(abstract_id) DO UPDATE SET artifact_id=excluded.artifact_id,status=excluded.status,reason=excluded.reason,error=excluded.error,updated=strftime('%Y-%m-%dT%H:%M:%fZ','now') RETURNING id",
        (paper_id, abstract['id'], pdf['id'] if pdf else None, status, reason, error),
    ).fetchone()[0]
    return dict(db.execute('SELECT * FROM full_text_checks WHERE id=?', (cid,)).fetchone())


def auto_check(db, assessment):
    if assessment['assessor']!='not-assessed' and assessment['stage']=='abstract' and (
            assessment['priority']=='high' or assessment['questions']):
        request_check(db, assessment['paper_id'], 'High relevance' if assessment['priority']=='high' else 'Decision-relevant uncertainty')


def text_list(body, name):
    values = body.get(name, [])
    if not isinstance(values, list) or len(values)>20 or any(not isinstance(s,str) or not s.strip() or len(s)>1000 for s in values):
        raise ValueError(name+' must contain at most 20 nonempty text entries')
    return [s.strip() for s in values]


def save_assessment(db, paper_id, body, assessor='owner'):
    if not isinstance(body, dict):
        raise ValueError('An assessment must be an object')
    paper, snapshot, digest, _ = subject(db, paper_id)
    stage = body.get('stage', 'abstract')
    if stage not in ('abstract','full_text'):
        raise ValueError('Choose abstract or full_text evidence')
    record = {}
    for name, choices in {'priority':PRIORITIES,'role':ROLES,'confidence':CONFIDENCES}.items():
        value = body.get(name)
        if not isinstance(value,str) or value not in choices or value=='unknown':
            raise ValueError('Choose a valid '+name)
        record[name] = value
    for name, maximum in (('explanation',5000),('limitation',3000),('source_version',200)):
        value = body.get(name, '')
        if not isinstance(value,str) or len(value)>maximum:
            raise ValueError(name+' must be text under '+str(maximum)+' characters')
        record[name] = value.strip()
    if not record['explanation']:
        raise ValueError('Explain the connection to the topic')
    record['questions'] = text_list(body, 'questions')
    record['sections'] = text_list(body, 'sections')
    evidence = body.get('evidence', [])
    if not isinstance(evidence,list) or len(evidence)>20:
        raise ValueError('evidence must be a list of at most 20 passages')
    baseline = ensure_abstract(db, paper_id)
    expected = body.get('expected_assessment_id')
    latest = current(db, paper_id)
    if expected is not None and expected!=latest['id']:
        raise HTTPException(409, 'The assessment changed; refresh before saving')
    compared_to, check = None, None
    if stage=='abstract':
        if record['sections'] or evidence:
            raise ValueError('Abstract-only assessments cannot include full-text evidence')
        record['source_url'] = paper['canonical_url']
        inferred = source_version(record['source_url'])
        if inferred and record['source_version'] and inferred!=record['source_version']:
            raise ValueError('The assessment version does not match the source URL')
        record['source_version'] = record['source_version'] or inferred
    else:
        if body.get('abstract_id')!=baseline['id']:
            raise HTTPException(409, 'The abstract assessment changed; refresh before saving the full-text check')
        check = db.execute('SELECT * FROM full_text_checks WHERE abstract_id=?', (baseline['id'],)).fetchone()
        if not check or check['status'] not in ('ready','checked'):
            raise HTTPException(409, 'Prepare the linked PDF before recording a full-text check')
        if record['source_version']!=baseline['source_version']:
            raise ValueError('Use the same source version as the abstract assessment')
        if not record['sections'] or not evidence:
            raise ValueError('A full-text check needs sections inspected and a supporting passage')
        record['source_url'] = db.execute('SELECT url FROM artifacts WHERE id=?', (check['artifact_id'],)).fetchone()[0]
        compared_to = baseline['id']
    passages = []
    for item in evidence:
        if not isinstance(item,dict) or type(item.get('page')) is not int or item['page']<1:
            raise ValueError('Each passage needs a PDF page number')
        quote = item.get('quote')
        if not isinstance(quote,str) or not 12 <= len(quote.strip()) <= 3000:
            raise ValueError('Each supporting passage must contain 12–3,000 characters')
        page = db.execute('SELECT text FROM pdf_text WHERE artifact_id=? AND page=?', (check['artifact_id'],item['page'])).fetchone()
        if not page or ' '.join(quote.split()) not in ' '.join(page[0].split()):
            raise ValueError('The supporting passage must occur on the cited PDF page')
        passages.append({'page':item['page'],'quote':quote.strip()})
    record.update(paper_id=paper_id,field=paper['field'],stage=stage,evidence=passages,assessor=assessor,
                  input_hash=digest,snapshot=snapshot,compared_to=compared_to)
    columns = list(record)
    encoded = [json.dumps(record[k],ensure_ascii=False) if isinstance(record[k],(list,dict)) else record[k] for k in columns]
    aid = db.execute('INSERT INTO review_assessments('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',encoded).lastrowid
    assessment = decode(db.execute('SELECT * FROM review_assessments WHERE id=?',(aid,)).fetchone())
    if stage=='full_text':
        db.execute("UPDATE full_text_checks SET status='checked',updated=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",(check['id'],))
    else:
        auto_check(db, assessment)
    return assessment


def prepare_pending(db):
    for row in db.execute("SELECT id FROM papers WHERE admission='pending'").fetchall():
        ensure_abstract(db,row['id'])


def finish_preparation(db, artifact_id, error=''):
    has_text = db.execute("SELECT 1 FROM pdf_text WHERE artifact_id=? AND length(trim(text))>0 LIMIT 1",(artifact_id,)).fetchone()
    if not error and not has_text:
        error = 'This PDF has no extractable text. Open the source to inspect it.'
    db.execute("UPDATE full_text_checks SET status=?,error=?,updated=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE artifact_id=? AND status IN ('queued','preparing')",
               ('unavailable' if error else 'ready',error[:1000],artifact_id))


def download_allowed(db, artifact_id):
    rows = db.execute("SELECT c.paper_id,c.abstract_id FROM full_text_checks c JOIN papers p ON p.id=c.paper_id WHERE c.artifact_id=? AND c.status IN ('queued','preparing') AND p.admission!='dismissed'",(artifact_id,)).fetchall()
    for row in rows:
        assessment = current(db,row['paper_id'])
        if assessment and (assessment['id'] if assessment['stage']=='abstract' else assessment['compared_to'])==row['abstract_id']:
            return True
    return False


def install_routes(app, settings):
    @app.post('/api/review/check')
    def check(body:dict):
        with connection(settings) as db:
            db.execute('BEGIN IMMEDIATE')
            return {'check':request_check(db,body.get('id'))}

    @app.post('/api/review/assessments')
    def assess(request:Request,body:dict):
        if 'expected_assessment_id' not in body:
            raise ValueError('Include expected_assessment_id from the displayed assessment or review work item')
        with connection(settings) as db:
            db.execute('BEGIN IMMEDIATE')
            return {'assessment':save_assessment(db,body.get('id'),body,'discovery-agent' if request.headers.get('authorization') else 'owner')}

    @app.get('/api/review/text')
    def full_text(id:str,page:int=1):
        with connection(settings,True) as db:
            review = summary(db,id)
            check = review['check']
            if not check or check['status'] not in ('ready','checked'):
                raise HTTPException(409,'The PDF is not ready to inspect')
            row = db.execute('SELECT text FROM pdf_text WHERE artifact_id=? AND page=?',(check['artifact_id'],page)).fetchone()
            if not row:
                raise HTTPException(404,'PDF page not found')
            count = db.execute('SELECT count(*) FROM pdf_text WHERE artifact_id=?',(check['artifact_id'],)).fetchone()[0]
            return {'page':page,'pages':count,'text':row[0],'artifact_id':check['artifact_id'],'abstract_id':review['abstract']['id']}

    @app.get('/api/review/work')
    def work(limit:int=10):
        if not 1 <= limit <= 50:
            raise ValueError('Choose a limit between 1 and 50')
        with connection(settings,True) as db:
            items = []
            for row in db.execute("SELECT id FROM papers WHERE admission='pending' OR (admission='approved' AND EXISTS (SELECT 1 FROM full_text_checks c WHERE c.paper_id=papers.id AND c.status='ready')) ORDER BY proposed_rel DESC NULLS LAST,added DESC"):
                review = summary(db,row['id'])
                assessment, check = review['current'], review['check']
                if not assessment or assessment['assessor']=='not-assessed' or (check and check['status']=='ready'):
                    items.append({'id':row['id'],'review':review})
                if len(items)>=limit:
                    break
            return {'items':items}
