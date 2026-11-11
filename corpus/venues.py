"""Find where arXiv preprints were eventually published.

arXiv only learns about publication when authors add a journal reference, which
few do, and ingest reads it once. This module asks OpenAlex and Semantic Scholar
(and Crossref for any DOI they name), applies well-corroborated answers, and
leaves the rest as proposals for the owner. A venue the owner edited is never
touched. Preprints are re-checked on a schedule because publication usually
comes months after the preprint.
"""
from __future__ import annotations

import asyncio, json, logging, re, time, urllib.parse
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Callable

from fastapi import HTTPException

from .db import connection, normalized_text, get_paper
from . import sources

log = logging.getLogger('corpus.venues')

Fetch = Callable[[str, dict], bytes]

# Venues common in this library, matched by name or by DBLP stream so that
# "neural information processing systems", "NeurIPS" and conf/nips agree.
KNOWN = [
    ('neurips', 'Advances in Neural Information Processing Systems', 'NeurIPS', 'conference', r'\bneurips\b|\bnips\b|neural information processing systems', 'conf/nips'),
    ('icml', 'International Conference on Machine Learning', 'ICML', 'conference', r'\bicml\b|international conference on machine learning', 'conf/icml'),
    ('iclr', 'International Conference on Learning Representations', 'ICLR', 'conference', r'\biclr\b|international conference on learning representations', 'conf/iclr'),
    ('aistats', 'International Conference on Artificial Intelligence and Statistics', 'AISTATS', 'conference', r'\baistats\b|artificial intelligence and statistics', 'conf/aistats'),
    ('uai', 'Conference on Uncertainty in Artificial Intelligence', 'UAI', 'conference', r'\buai\b|uncertainty in artificial intelligence', 'conf/uai'),
    ('cvpr', 'IEEE/CVF Conference on Computer Vision and Pattern Recognition', 'CVPR', 'conference', r'\bcvpr\b|computer vision and pattern recognition', 'conf/cvpr'),
    ('iccv', 'IEEE/CVF International Conference on Computer Vision', 'ICCV', 'conference', r'\biccv\b|international conference on computer vision', 'conf/iccv'),
    ('eccv', 'European Conference on Computer Vision', 'ECCV', 'conference', r'\beccv\b|european conference on computer vision', 'conf/eccv'),
    ('aaai', 'AAAI Conference on Artificial Intelligence', 'AAAI', 'conference', r'\baaai\b', 'conf/aaai'),
    ('ijcai', 'International Joint Conference on Artificial Intelligence', 'IJCAI', 'conference', r'\bijcai\b|international joint conference on artificial intelligence', 'conf/ijcai'),
    ('colt', 'Conference on Learning Theory', 'COLT', 'conference', r'\bcolt\b|conference on learning theory', 'conf/colt'),
    ('corl', 'Conference on Robot Learning', 'CoRL', 'conference', r'\bcorl\b|conference on robot learning', 'conf/corl'),
    ('l4dc', 'Learning for Dynamics and Control Conference', 'L4DC', 'conference', r'\bl4dc\b|learning for dynamics (?:and|&) control', 'conf/l4dc'),
    ('tmlr', 'Transactions on Machine Learning Research', 'TMLR', 'journal', r'\btmlr\b|transactions on machine learning research', 'journals/tmlr'),
    ('jmlr', 'Journal of Machine Learning Research', 'JMLR', 'journal', r'\bjmlr\b|journal of machine learning research', 'journals/jmlr'),
]
_SERIES = re.compile(r'^(lecture notes in|communications in computer and information science|proceedings of machine learning research)', re.I)
_PREPRINT = re.compile(r'arxiv|biorxiv|medrxiv|chemrxiv|ssrn|research square|preprints?\.org|\bcorr\b', re.I)
_WORKSHOP = re.compile(r'workshop', re.I)


@dataclass
class Candidate:
    source: str
    venue: str
    key: str
    venue_class: str
    year: int | None = None
    year_sure: bool = False   # the year comes from the venue record, not the preprint
    doi: str | None = None
    url: str = ''
    workshop: bool = False
    strong: bool = False      # a curated record (Crossref DOI or DBLP entry) backs it


@dataclass
class Result:
    status: str               # 'applied' | 'proposed' | 'none' | 'rejected-before' | 'error'
    candidate: Candidate | None = None
    sources: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _http(url, headers):
    return sources._fetch_url(url, max_bytes=4 * 1024 * 1024, timeout=20, headers={'Accept': 'application/json', **headers})


def _json(fetch, url, headers=None):
    """GET JSON; None for 404 (the source doesn't know the paper)."""
    try:
        return json.loads(fetch(url, headers or {}).decode('utf-8'))
    except sources.SourceError as e:
        if 'HTTP 404' in str(e): return None
        raise


def known_venue(name='', dblp=''):
    for key, full, short, cls, pattern, stream in KNOWN:
        if (dblp and dblp.startswith(stream + '/')) or (name and re.search(pattern, name, re.I)):
            return key, f'{full} ({short})', cls
    return None


def make_candidate(source, name, *, venue_class='', year=None, year_sure=False, doi=None, url='', dblp='', strong=False):
    name = sources._clean_text(name)
    if (not name and not dblp) or _PREPRINT.search(name or '') or dblp.startswith('journals/corr/'):
        return None
    workshop = bool(_WORKSHOP.search(name))
    known = None if workshop else known_venue(name, dblp)
    if known:
        key, venue, cls = known
    elif not name:
        return None
    else:
        key, venue = normalized_text(name), name
        cls = venue_class if venue_class in ('conference', 'journal') else 'conference' if re.search(r'conference|proceedings|symposium', name, re.I) else 'journal' if re.search(r'journal|transactions|letters|review', name, re.I) else 'other'
    if workshop:
        cls = 'other'
    return Candidate(source, venue, key, cls, year, year_sure, doi, url, workshop, strong)


def _titles_match(a, b):
    a, b = normalized_text(a or ''), normalized_text(b or '')
    return bool(a and b) and SequenceMatcher(None, a, b).ratio() >= 0.85


def _year_in_url(url):
    match = re.search(r'/((?:19|20)\d\d)/', url or '')
    return int(match.group(1)) if match else None


def from_crossref(fetch, doi, title):
    """A DOI is the strongest evidence when its record carries this paper's title.
    Titles do change at publication, so a different title still yields a
    candidate, just not one strong enough to apply unasked."""
    data = _json(fetch, 'https://api.crossref.org/works/' + urllib.parse.quote(doi, safe=''))
    message = (data or {}).get('message')
    if not isinstance(message, dict) or message.get('type') == 'posted-content':
        return None
    titles = message.get('title') or ['']
    same_title = _titles_match(titles[0], title)
    containers = [sources._clean_text(c) for c in message.get('container-title') or [] if c]
    named = [c for c in containers if known_venue(c)] or [c for c in containers if not _SERIES.search(c)] or containers
    kind = message.get('type', '')
    cls = 'journal' if kind == 'journal-article' else 'conference' if kind in ('proceedings-article', 'book-chapter') else ''
    year = sources._year_from_crossref(message)
    return make_candidate('crossref', named[0] if named else '', venue_class=cls, year=year, year_sure=year is not None, doi=doi, url='https://doi.org/' + doi, strong=same_title)


def from_openalex(fetch, arxiv_id, title, api_key=''):
    url = 'https://api.openalex.org/works/doi:10.48550/arXiv.' + urllib.parse.quote(arxiv_id, safe='/') + '?select=id,title,publication_year,locations'
    if api_key: url += '&api_key=' + urllib.parse.quote(api_key)
    # Looked up by the arXiv DOI, so the work is this paper even when OpenAlex
    # shows it under its published title; Crossref then judges each DOI.
    work = _json(fetch, url)
    if not work:
        return [], []
    found, dois = [], []
    for location in work.get('locations') or []:
        source = location.get('source') or {}
        if source.get('type') not in ('journal', 'conference', 'book series'):
            continue
        landing = location.get('landing_page_url') or ''
        doi = sources._clean_doi(landing) if 'doi.org/' in landing else None
        if doi and not doi.lower().startswith('10.48550/'):
            dois.append(doi); continue
        year = _year_in_url(landing)
        candidate = make_candidate('openalex', source.get('display_name', ''), venue_class=source.get('type', ''),
                                   year=year or work.get('publication_year'), year_sure=year is not None, url=landing)
        if candidate: found.append(candidate)
    return found, dois


def from_semantic_scholar(fetch, arxiv_id, title, api_key=''):
    url = 'https://api.semanticscholar.org/graph/v1/paper/arXiv:' + urllib.parse.quote(arxiv_id, safe='/') + '?fields=title,venue,year,publicationVenue,externalIds'
    paper = _json(fetch, url, {'x-api-key': api_key} if api_key else {})
    if not paper:
        return [], []
    same_title = _titles_match(paper.get('title'), title)
    ids = paper.get('externalIds') or {}
    venue = (paper.get('publicationVenue') or {}).get('name') or paper.get('venue') or ''
    found = []
    dblp = ids.get('DBLP') or ''
    match = re.search(r'(\d\d)[a-z]?$', dblp)
    if dblp and match and not dblp.startswith('journals/corr/') and not _WORKSHOP.search(venue):
        # DBLP is curated, and its key carries the venue stream and the year of
        # the proceedings (conf/iclr/LovelaceT21), which S2's own year is not.
        yy = int(match.group(1)); this = datetime.now(timezone.utc).year % 100
        candidate = make_candidate('dblp', venue, dblp=dblp, year=(2000 if yy <= this + 1 else 1900) + yy, year_sure=True,
                                   url='https://dblp.org/rec/' + dblp, strong=same_title)
        if candidate: found.append(candidate)
    elif venue:
        candidate = make_candidate('semanticscholar', venue, venue_class=((paper.get('publicationVenue') or {}).get('type') or ''),
                                   year=paper.get('year'), url='https://www.semanticscholar.org/arxiv/' + arxiv_id)
        if candidate: found.append(candidate)
    doi = sources._clean_doi(ids.get('DOI') or '')
    return found, [doi] if doi and not doi.lower().startswith('10.48550/') else []


def gather(paper, settings, fetch: Fetch | None = None):
    fetch = fetch or _http
    """Every published-version candidate the sources offer, plus any source errors."""
    arxiv_id, title = paper['arxiv_id'], paper['title']
    candidates, dois, errors = [], [], []
    for name, lookup, key in (('OpenAlex', from_openalex, settings.openalex_api_key), ('Semantic Scholar', from_semantic_scholar, settings.s2_api_key)):
        try:
            found, more = lookup(fetch, arxiv_id, title, key) or ([], [])
            candidates += found; dois += more
        except Exception as e:  # one source failing must not hide the others
            errors.append(f'{name}: {str(e)[:200]}')
    for doi in dict.fromkeys(d.lower() for d in dois):
        try:
            candidate = from_crossref(fetch, doi, title)
            if candidate: candidates.append(candidate)
        except Exception as e:
            errors.append(f'Crossref: {str(e)[:200]}')
    return candidates, errors


def decide(candidates, preprint_year=None):
    """Pick the best-supported venue and whether it is safe to apply unasked.

    Applied: no other non-workshop venue disagrees, it is not a workshop, and it
    is backed by a curated record (a Crossref DOI or a DBLP entry) or by two
    independent sources. Everything else becomes a proposal."""
    groups = {}
    for c in candidates:
        groups.setdefault(c.key, []).append(c)
    if not groups:
        return None, False, []

    def support(items):
        return (len({c.source for c in items}), any(c.strong for c in items), any(c.year_sure for c in items))
    key = max(groups, key=lambda k: support(groups[k]))
    items = groups[key]
    best = next((c for c in items if c.source == 'crossref'), None) or next((c for c in items if c.source == 'dblp'), None) or items[0]
    # Sources that only know the preprint report its year; leave the year
    # unconfirmed rather than propose the wrong one.
    sure = [c.year for c in items if c.year_sure and c.year]
    year = next((c.year for c in items if c.source == 'crossref' and c.year_sure and c.year), None) or (max(sure) if sure else None)
    doi = next((c.doi for c in items if c.doi), None)
    chosen = Candidate(best.source, best.venue, key, best.venue_class, year, bool(sure), doi,
                       next((c.url for c in items if c.doi), best.url), best.workshop, any(c.strong for c in items))
    # Free-text names from a single weak source vary too much ("Phys. Rev. Lett.")
    # to veto a curated record; a known venue or a curated record does.
    rivals = [k for k, group in groups.items() if k != key and not all(c.workshop for c in group)
              and (any(c.strong for c in group) or known_venue(group[0].venue) or not chosen.strong)]
    sources_used = sorted({c.source for c in items})
    plausible_year = year is not None and (preprint_year is None or year >= preprint_year - 1)
    auto = (not chosen.workshop and not rivals and plausible_year
            and (chosen.strong or len(sources_used) >= 2))
    return chosen, auto, sources_used


SOURCE_NAMES = {'crossref': 'Crossref', 'dblp': 'DBLP', 'openalex': 'OpenAlex', 'semanticscholar': 'Semantic Scholar'}


def check(db, paper_id, settings, fetch: Fetch | None = None):
    """Look up one paper and record the outcome. Network calls happen before the
    write transaction so a slow source never holds the database lock."""
    paper = db.execute('SELECT id,arxiv_id,title,year,venue_class,venue_source FROM papers WHERE id=?', (paper_id,)).fetchone()
    if not paper:
        raise HTTPException(404, 'Paper not found')
    if not paper['arxiv_id']:
        raise ValueError('Only arXiv papers can be checked for a published version')
    candidates, errors = gather(dict(paper), settings, fetch)
    chosen, auto, used = decide(candidates, paper['year'])
    result = Result('none', chosen, used, [asdict(c) for c in candidates], errors)
    db.execute('BEGIN IMMEDIATE')
    current = db.execute('SELECT venue_class,venue_source FROM papers WHERE id=?', (paper_id,)).fetchone()
    if not current:
        raise HTTPException(404, 'Paper not found')
    # A source that failed may be the one that knows the answer, so a check
    # with errors and nothing found is retried the next day (see DUE).
    failed = bool(errors) and not candidates
    db.execute('UPDATE papers SET venue_checked=?,venue_check_error=? WHERE id=?', (now(), '; '.join(errors) if failed else None, paper_id))
    if failed:
        result.status = 'error'
        return result
    # Never override the owner, and leave papers that already left preprint alone.
    if not chosen or current['venue_source'] == 'manual' or current['venue_class'] != 'preprint':
        return result
    previous = db.execute('SELECT id,status FROM venue_proposals WHERE paper_id=? AND venue_key=? AND year IS ?', (paper_id, chosen.key, chosen.year)).fetchone()
    if previous and previous['status'] == 'rejected':
        result.status = 'rejected-before'
        return result
    db.execute("DELETE FROM venue_proposals WHERE paper_id=? AND status='pending' AND id IS NOT ?", (paper_id, previous['id'] if previous else None))
    values = (chosen.key, chosen.venue, chosen.venue_class, chosen.year, chosen.doi, chosen.url, json.dumps(used), json.dumps(result.candidates, ensure_ascii=False))
    if previous:
        db.execute("UPDATE venue_proposals SET venue_key=?,venue=?,venue_class=?,year=?,doi=?,url=?,sources=?,candidates=?,status='pending',decided=NULL WHERE id=?", (*values, previous['id']))
        proposal_id = previous['id']
    else:
        proposal_id = db.execute('INSERT INTO venue_proposals(venue_key,venue,venue_class,year,doi,url,sources,candidates,paper_id) VALUES(?,?,?,?,?,?,?,?,?)', (*values, paper_id)).lastrowid
    if auto:
        apply(db, proposal_id, 'auto:' + '+'.join(used))
        result.status = 'applied'
    else:
        result.status = 'proposed'
    return result


def apply(db, proposal_id, source='manual'):
    proposal = db.execute('SELECT * FROM venue_proposals WHERE id=?', (proposal_id,)).fetchone()
    if not proposal:
        raise HTTPException(404, 'Proposal not found')
    if proposal['status'] != 'pending':
        raise HTTPException(409, 'This proposal was already decided; refresh the paper')
    paper = db.execute('SELECT doi FROM papers WHERE id=?', (proposal['paper_id'],)).fetchone()
    doi = paper['doi']
    if not doi and proposal['doi'] and not db.execute('SELECT 1 FROM papers WHERE doi=?', (proposal['doi'],)).fetchone():
        doi = proposal['doi']   # doi is UNIQUE; another paper may already hold the published DOI
    db.execute("UPDATE papers SET venue=?,venue_class=?,year=coalesce(?,year),doi=?,venue_source=?,modified=? WHERE id=?",
               (proposal['venue'], proposal['venue_class'], proposal['year'], doi, source, now(), proposal['paper_id']))
    db.execute("UPDATE venue_proposals SET status='applied',decided=? WHERE id=?", (now(), proposal_id))


def reject(db, proposal_id):
    proposal = db.execute('SELECT status FROM venue_proposals WHERE id=?', (proposal_id,)).fetchone()
    if not proposal:
        raise HTTPException(404, 'Proposal not found')
    if proposal['status'] != 'pending':
        raise HTTPException(409, 'This proposal was already decided; refresh the paper')
    db.execute("UPDATE venue_proposals SET status='rejected',decided=? WHERE id=?", (now(), proposal_id))


def pending(db, paper_id):
    row = db.execute("SELECT * FROM venue_proposals WHERE paper_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (paper_id,)).fetchone()
    if not row:
        return None
    proposal = dict(row)
    proposal['sources'] = [SOURCE_NAMES.get(s, s) for s in json.loads(proposal['sources'])]
    proposal['candidates'] = json.loads(proposal['candidates'])
    return proposal


# A preprint from the last two years is checked weekly, older ones monthly, and
# one whose sources failed the next day; a paper waiting on the owner's
# decision is not re-checked until it is decided.
DUE = """SELECT id FROM papers p
 WHERE admission='approved' AND arxiv_id IS NOT NULL AND venue_class='preprint' AND venue_source!='manual'
 AND NOT EXISTS (SELECT 1 FROM venue_proposals v WHERE v.paper_id=p.id AND v.status='pending')
 AND (venue_checked IS NULL OR julianday('now')-julianday(venue_checked) >
      CASE WHEN venue_check_error IS NOT NULL THEN 1
           WHEN coalesce(year,0) >= cast(strftime('%Y','now') AS INTEGER)-1 THEN 7 ELSE 30 END)
 ORDER BY venue_checked IS NOT NULL, venue_checked, added DESC LIMIT 1"""


def check_next(settings, fetch: Fetch | None = None):
    with connection(settings) as db:
        row = db.execute(DUE).fetchone()
        if not row:
            return None
        result = check(db, row['id'], settings, fetch)
        if result.status in ('applied', 'proposed'):
            log.info('Published version %s for %s: %s %s', result.status, row['id'], result.candidate.venue, result.candidate.year)
        return result


async def worker(settings, stop):
    async def pause(seconds):
        try: await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError: pass
    await pause(60)   # let startup settle; also keeps short-lived app instances off the network
    while not stop.is_set():
        try:
            result = await asyncio.to_thread(check_next, settings)
        except Exception:
            log.exception('Published-version check failed')
            result = None
        await pause(settings.venue_check_interval if result else 900)


def install_routes(app, settings, limits):
    @app.post('/api/papers/venue-check')
    def check_now(body: dict):
        if not limits.allow('venue-check', 10, 60):
            raise HTTPException(429, 'At most 10 published-version checks per minute')
        paper_id = body.get('id')
        if not isinstance(paper_id, str):
            raise ValueError('Choose a paper')
        with connection(settings) as db:
            result = check(db, paper_id, settings)
        with connection(settings, True) as db:
            return {'status': result.status, 'errors': result.errors, 'paper': get_paper(db, paper_id)}

    @app.post('/api/venue-proposals/{proposal_id}')
    def decide_proposal(proposal_id: int, body: dict):
        action = body.get('action')
        if action not in ('apply', 'reject'):
            raise ValueError('Choose apply or reject')
        with connection(settings) as db:
            db.execute('BEGIN IMMEDIATE')
            (apply if action == 'apply' else reject)(db, proposal_id)
            paper_id = db.execute('SELECT paper_id FROM venue_proposals WHERE id=?', (proposal_id,)).fetchone()[0]
            return {'paper': get_paper(db, paper_id)}


def main():
    import argparse
    from .config import Settings
    from .db import initialize
    parser = argparse.ArgumentParser(description='Check arXiv preprints for a published version')
    parser.add_argument('--all-due', action='store_true', help='check every paper that is due, then exit')
    parser.add_argument('--paper', help='check one paper by id')
    args = parser.parse_args()
    settings = Settings(); initialize(settings)
    if args.paper:
        with connection(settings) as db:
            result = check(db, args.paper, settings)
        print(result.status, result.candidate and f'{result.candidate.venue} {result.candidate.year}', *result.errors)
        return
    while args.all_due and (result := check_next(settings)):
        print(result.status, result.candidate and f'{result.candidate.venue} {result.candidate.year}', *result.errors)
        time.sleep(settings.venue_check_interval)


if __name__ == '__main__':
    main()
