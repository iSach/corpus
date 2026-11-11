"""Human approval is independent of reading and reviewing an admitted paper."""
import argparse
from datetime import datetime, timezone, timedelta
import json

from fastapi import HTTPException

from .db import connection, get_paper
from .query import compile_query


def counts(db):
    result = {'pending':0, 'approved':0, 'dismissed':0, 'later':0}
    for row in db.execute("SELECT CASE WHEN admission='pending' AND datetime(deferred_until)>datetime('now') THEN 'later' ELSE admission END view,count(*) FROM papers WHERE admission!='approved' OR EXISTS (SELECT 1 FROM inbox_decisions d WHERE d.paper_id=papers.id) GROUP BY view"):
        result[row[0]] = row[1]
    return result


def listing(db, query='', field='', status='pending', sort='relevance', limit=50, offset=0, evidence='', origin=''):
    if status not in ('pending','approved','dismissed','later') or sort not in ('relevance','newest','oldest'):
        raise ValueError('Invalid inbox view')
    if evidence not in ('','unassessed','abstract','full_text','ready','unavailable') or origin not in ('','existing','discovery'):
        raise ValueError('Invalid evidence or discovery filter')
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError('Invalid pagination')
    compiled = compile_query(query, {'field':[field]} if field else {})
    where = 'p.admission=? AND ('+compiled.where+')'
    params = ['pending' if status=='later' else status, *compiled.params]
    if status=='later':
        where += " AND datetime(p.deferred_until)>datetime('now')"
    elif status=='pending':
        where += " AND (p.deferred_until IS NULL OR datetime(p.deferred_until)<=datetime('now'))"
    if origin:
        where += ' AND p.inbox_origin=?'
        params.append(origin)
    if evidence=='unassessed':
        where += " AND (a.id IS NULL OR a.assessor='not-assessed')"
    elif evidence in ('abstract','full_text'):
        where += " AND a.stage=? AND a.assessor!='not-assessed'"
        params.append(evidence)
    elif evidence:
        where += ' AND c.status=?'
        params.append(evidence)
    if status == 'approved':
        where += ' AND EXISTS (SELECT 1 FROM inbox_decisions d WHERE d.paper_id=p.id)'
    order = {'relevance':"CASE a.priority WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 WHEN 'exclude' THEN 0 END DESC NULLS LAST,p.proposed_rel DESC NULLS LAST,p.added DESC", 'newest':'p.added DESC', 'oldest':'p.added ASC'}[sort]
    joins = ' FROM papers p JOIN ingest_run r ON r.id=p.ingest_run_id LEFT JOIN review_assessments a ON a.id=(SELECT id FROM review_assessments x WHERE x.paper_id=p.id AND x.field=p.field ORDER BY x.id DESC LIMIT 1) LEFT JOIN full_text_checks c ON c.abstract_id=CASE WHEN a.stage=\'abstract\' THEN a.id ELSE a.compared_to END '
    total = db.execute('SELECT count(*)'+joins+'WHERE '+where, params).fetchone()[0]
    rows = db.execute(
        "SELECT p.id,p.title,p.authors,p.venue,p.year,p.field,p.added,p.modified,p.admission,p.proposed_rel,p.inbox_origin,p.deferred_until,r.agent,a.priority,a.stage,a.confidence,a.assessor,coalesce(a.id,0)||':'||coalesce(c.status,'')||':'||coalesce(c.updated,'') review_revision "+joins+'WHERE '+where+' ORDER BY '+order+',p.id LIMIT ? OFFSET ?',
        [*params, limit, offset],
    ).fetchall()
    papers = [{**dict(row), 'authors':json.loads(row['authors'])} for row in rows]
    return {'papers':papers, 'total':total, 'counts':counts(db)}


def set_admission(db, paper, admission, undo_of=None, assessment_id=None, deferred_until=None):
    modified = datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
    db.execute('UPDATE papers SET admission=?,modified=?,deferred_until=? WHERE id=?', (admission,modified,deferred_until,paper['id']))
    if admission == 'approved':
        db.execute("UPDATE artifacts SET fetch_status='pending',error=NULL WHERE paper_id=? AND kind='pdf' AND fetch_status IN ('linked','failed')", (paper['id'],))
    else:
        db.execute("UPDATE artifacts SET fetch_status='linked' WHERE paper_id=? AND kind='pdf' AND fetch_status IN ('pending','fetching')", (paper['id'],))
    decision = db.execute(
        'INSERT INTO inbox_decisions(paper_id,before_admission,after_admission,paper_modified,undo_of,assessment_id,before_deferred_until,after_deferred_until) VALUES(?,?,?,?,?,?,?,?)',
        (paper['id'],paper['admission'],admission,modified,undo_of,assessment_id,dict(paper).get('deferred_until'),deferred_until),
    ).lastrowid
    return {'id':decision, 'paper_id':paper['id'], 'before':paper['admission'], 'after':admission}


def decide(db, paper_id, action, expected_modified=None, assessment_id=None):
    transitions = {'approve':('pending','approved'), 'dismiss':('pending','dismissed'),
                   'restore':('dismissed','pending'), 'return':('approved','pending'),
                   'defer':('pending','pending'), 'resume':('pending','pending')}
    if not isinstance(action,str) or action not in transitions or not isinstance(paper_id,str):
        raise ValueError('Choose a paper and a valid inbox action')
    db.execute('BEGIN IMMEDIATE')
    paper = db.execute('SELECT id,admission,modified,deferred_until FROM papers WHERE id=?', (paper_id,)).fetchone()
    if not paper:
        raise HTTPException(404,'Paper not found')
    before, after = transitions[action]
    if paper['admission'] != before or (expected_modified is not None and expected_modified != paper['modified']):
        raise HTTPException(409,'This paper changed; refresh the Inbox before deciding')
    from .review import current
    latest = current(db,paper_id)
    if assessment_id is not None:
        # Record the assessment actually displayed, even if a later check arrived
        # between rendering the card and clicking Approve from abstract.
        evidence = db.execute('SELECT * FROM review_assessments WHERE id=? AND paper_id=?',(assessment_id,paper_id)).fetchone()
        if not evidence or not latest or evidence['input_hash']!=latest['input_hash']:
            raise HTTPException(409,'The evidence changed; refresh before deciding')
    else:
        assessment_id = latest['id'] if latest else None
    defer = (datetime.now(timezone.utc)+timedelta(days=1)).isoformat() if action=='defer' else None
    result = set_admission(db,paper,after,assessment_id=assessment_id,deferred_until=defer)
    if after=='pending':
        resume_review(db,paper_id)
    return result


def resume_review(db, paper_id):
    """set_admission pauses PDF work for pending papers; restart an unfinished full-text check."""
    from .review import ensure_abstract, summary, request_check
    ensure_abstract(db,paper_id)
    review = summary(db,paper_id)
    if review['check'] and review['check']['status'] in ('queued','preparing'):
        request_check(db,paper_id,review['check']['reason'])


def undo(db, decision_id):
    if type(decision_id) is not int:
        raise ValueError('Choose a valid decision to undo')
    db.execute('BEGIN IMMEDIATE')
    decision = db.execute('SELECT * FROM inbox_decisions WHERE id=?', (decision_id,)).fetchone()
    if not decision:
        raise HTTPException(404,'Decision not found')
    paper = db.execute('SELECT id,admission,modified,deferred_until FROM papers WHERE id=?', (decision['paper_id'],)).fetchone()
    latest = db.execute('SELECT max(id) FROM inbox_decisions WHERE paper_id=?', (decision['paper_id'],)).fetchone()[0]
    if (not paper or latest != decision_id or paper['admission'] != decision['after_admission']
            or paper['modified'] != decision['paper_modified'] or decision['undo_of'] is not None):
        raise HTTPException(409,'This decision can no longer be undone; the paper changed')
    result = set_admission(db,paper,decision['before_admission'],decision_id,deferred_until=decision['before_deferred_until'])
    if decision['before_admission']=='pending':
        resume_review(db,paper['id'])
    return result


def stage_agent(db, agent):
    """Backfill only unreviewed imports from an explicitly selected agent."""
    db.execute('BEGIN IMMEDIATE')
    rows = db.execute("SELECT p.id,p.admission FROM papers p JOIN ingest_run r ON r.id=p.ingest_run_id WHERE r.agent=? AND p.state='unreviewed' AND p.admission='approved' AND NOT EXISTS (SELECT 1 FROM inbox_decisions d WHERE d.paper_id=p.id)", (agent,)).fetchall()
    for paper in rows:
        set_admission(db, paper, 'pending')
        db.execute("UPDATE papers SET inbox_origin='existing' WHERE id=?",(paper['id'],))
        from .review import ensure_abstract
        ensure_abstract(db,paper['id'])
    return len(rows)


def install_routes(app, settings):
    @app.get('/api/inbox')
    def inbox(q:str='', field:str='', status:str='pending', sort:str='relevance', limit:int=50, offset:int=0, evidence:str='', origin:str=''):
        with connection(settings,True) as db:
            return listing(db,q,field,status,sort,limit,offset,evidence,origin)

    @app.post('/api/inbox/decision')
    def decision(body:dict):
        with connection(settings) as db:
            result = decide(db,body.get('id'),body.get('action'),body.get('expected_modified'),body.get('assessment_id'))
            return {'decision':result, 'paper':get_paper(db,result['paper_id'])}

    @app.post('/api/inbox/undo')
    def undo_decision(body:dict):
        with connection(settings) as db:
            result = undo(db,body.get('decision_id'))
            return {'decision':result}


def main():
    from .config import Settings
    from .db import initialize
    parser = argparse.ArgumentParser(description='Move existing automatic imports to the approval inbox')
    parser.add_argument('--stage-agent', required=True)
    args = parser.parse_args()
    settings = Settings()
    initialize(settings)
    with connection(settings) as db:
        changed = stage_agent(db,args.stage_agent)
    print('Moved '+str(changed)+' unreviewed imports to the Inbox.')


if __name__ == '__main__':
    main()
