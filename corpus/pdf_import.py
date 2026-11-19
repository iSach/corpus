"""Turn an uploaded PDF into a paper: read it, find the published record, keep the file.

The analysis is local and deliberately conservative.  The identifiers a paper
prints about itself (the arXiv margin stamp, a DOI in its metadata or on its
first page) are the strongest evidence; failing those, the title set in the
largest type on the first page is searched for.  When nothing matches, the
entry is built from what the PDF says about itself.
"""
import hashlib, io, math, os, re, time, unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from . import sources
from .db import normalized_text

UPLOAD_RE = re.compile(r'^upload:([0-9a-f]{64})$')
_TOKEN_RE = re.compile(r'^[0-9a-f]{64}$')
_ARXIV_STAMP_RE = re.compile(r'arXiv:\s*(\d{4}\.\d{4,5}|[a-z][a-z0-9-]*(?:\.[A-Z]{2})?/\d{7})(v\d+)?', re.I)
_DOI_TEXT_RE = re.compile(r'\b10\.\d{4,9}/[^\s"<>{}]+')
_ABSTRACT_RE = re.compile(r'(?:^|\n)\s*A\s?(?:BSTRACT|bstract)\b[\s.:—–-]*', re.M)
_ABSTRACT_END_RE = re.compile(r'\n\s*(?:(?:1|I)\.?\s*)?(?:INTRODUCTION|Introduction)\b|\n\s*(?:Keywords|Key words|Index Terms|CCS Concepts)\b')
_PLACEHOLDER_TITLE_RE = re.compile(r'^(?:untitled|microsoft word|document\d*|title)\b|\.(?:docx?|tex|dvi|pdf)$', re.I)
MAX_PAGES = 1000
MAX_TEXT = 20_000_000
TITLE_MATCH = .9      # title similarity accepted from a title search
TITLE_CONFIRMS = .75  # similarity that confirms a DOI read from the page text


class PdfImportError(ValueError):
    pass


def title_similarity(a, b):
    a, b = normalized_text(a or ''), normalized_text(b or '')
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.


def _reader(data):
    from pypdf import PdfReader
    if not data[:1024].lstrip().startswith(b'%PDF-') and b'%PDF-' not in data[:1024]:
        raise PdfImportError('This file is not a PDF')
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = len(reader.pages)
    except Exception as error:
        raise PdfImportError('This PDF could not be read: '+str(error)[:200])
    if not pages: raise PdfImportError('This PDF has no pages')
    if pages > MAX_PAGES: raise PdfImportError('PDF exceeds the 1,000-page limit')
    return reader


def _clean(text):
    return (text or '').replace('\x00', '').replace('\x01', '').replace('\x02', '')


def _readable(text):
    """Undo typesetting ligatures (ﬁ, ﬂ) in text shown to people."""
    return ' '.join(unicodedata.normalize('NFKC', text or '').split())


def page_texts(data):
    """All page texts, bounded like the download worker's."""
    reader = _reader(data); pages = []; total = 0
    for page in reader.pages:
        try: text = _clean(page.extract_text())
        except Exception: text = ''
        total += len(text)
        if total > MAX_TEXT: raise PdfImportError('Extracted PDF text exceeds 20 million characters')
        pages.append(text)
    return pages


def _multiply(a, b):
    return [a[0]*b[0]+a[1]*b[2], a[0]*b[1]+a[1]*b[3], a[2]*b[0]+a[3]*b[2], a[2]*b[1]+a[3]*b[3],
            a[4]*b[0]+a[5]*b[2]+b[4], a[4]*b[1]+a[5]*b[3]+b[5]]


def _title_from_layout(page):
    """The text set in the largest upright type in the upper part of the page."""
    runs = []
    try: height = float(page.mediabox.height) or 792.
    except Exception: height = 792.

    def visit(text, cm, tm, font, size):
        if not text or not text.strip(): return
        try:
            m = _multiply([float(v) for v in tm], [float(v) for v in cm])
            scale = math.hypot(m[2], m[3]); effective = float(size or 0)*scale
        except (TypeError, ValueError):
            return
        upright = abs(m[1]) < 1e-3 and abs(m[2]) < 1e-3 and m[3] > 0
        if effective <= 0 or not upright or m[5] < height*.35: return
        runs.append((round(effective*2)/2, text))
    try: page.extract_text(visitor_text=visit)
    except Exception: return ''
    for size in sorted({size for size, _ in runs}, reverse=True)[:4]:
        text = ' '.join(' '.join(t for s, t in runs if s == size).split())
        text = _ARXIV_STAMP_RE.sub('', text).strip(' .,;:')
        words = len(text.split())
        if 3 <= words <= 40 and 10 <= len(text) <= 400 and re.search(r'[^\W\d_]{3}', text):
            return text
    return ''


def _metadata_text(reader, key):
    try:
        value = (reader.metadata or {}).get(key)
    except Exception:
        return ''
    return ' '.join(str(value or '').split())


def _xmp_doi(reader):
    try:
        stream = reader.trailer['/Root'].get('/Metadata')
        payload = stream.get_object().get_data().decode('utf-8', 'replace')[:200_000] if stream else ''
    except Exception:
        return None
    match = re.search(r'<prism:doi>\s*([^<\s]+)\s*<', payload) or re.search(r'prism:doi="([^"]+)"', payload) \
        or re.search(r'<dc:identifier>\s*(?:doi:)?(10\.[^<\s]+)\s*<', payload)
    return sources._clean_doi(match.group(1)) if match else None


def _authors(value):
    if not value or len(value) > 2000: return []
    value = re.sub(r'\([^)]*\)|<[^>]*>|\S+@\S+', ' ', value)
    parts = re.split(r'\s*;\s*|\s+and\s+|\s*,\s*(?=[^,]+\s[^,]+)', value)
    names = [' '.join(p.split()).strip(' ,') for p in parts if 2 <= len(p.strip(' ,')) <= 120]
    return names[:100] if all(re.search(r'[^\W\d_]', n) for n in names) else []


def _abstract(text):
    match = _ABSTRACT_RE.search(text)
    if not match: return ''
    rest = text[match.end():match.end()+8000]
    end = _ABSTRACT_END_RE.search(rest)
    body = rest[:end.start()] if end else rest[:3000]
    body = re.sub(r'-\n(?=[a-z])', '', body)
    return _readable(body)[:5000]


def _year(reader, arxiv_id):
    if arxiv_id and re.match(r'\d{4}\.', arxiv_id):
        return 2000+int(arxiv_id[:2])
    created = _metadata_text(reader, '/CreationDate')
    match = re.match(r'(?:D:)?((?:19|20)\d{2})', created)
    return int(match.group(1)) if match else None


def analyze(data, filename=''):
    """What the PDF says about itself: identifiers, a title guess and metadata."""
    reader = _reader(data)
    first = reader.pages[0]
    try: text = _clean(first.extract_text())
    except Exception: text = ''
    second = ''
    if len(reader.pages) > 1:
        try: second = _clean(reader.pages[1].extract_text())
        except Exception: pass
    stamp = _ARXIV_STAMP_RE.search(text)
    arxiv_id = stamp.group(1).lower() if stamp else None
    arxiv_version = (stamp.group(2) or '').lower() if stamp else ''
    meta_doi = sources._clean_doi(_metadata_text(reader, '/doi') or _metadata_text(reader, '/DOI') or '') or _xmp_doi(reader)
    text_doi = None
    for match in _DOI_TEXT_RE.finditer(text):
        candidate = sources._clean_doi(match.group(0))
        if candidate and not candidate.startswith('10.48550/'):
            text_doi = candidate; break
    if not arxiv_id:
        for doi in (meta_doi, text_doi):
            if doi and doi.startswith('10.48550/arxiv.'):
                arxiv_id = doi[len('10.48550/arxiv.'):]
    layout_title = _title_from_layout(first)
    meta_title = _metadata_text(reader, '/Title')
    if meta_title and (_PLACEHOLDER_TITLE_RE.search(meta_title) or len(meta_title.split()) < 2 or len(meta_title) > 400):
        meta_title = ''
    # Authors' own metadata is usually right when present; layout is the fallback.
    title = meta_title if meta_title and (not layout_title or title_similarity(meta_title, layout_title) > .6) else layout_title or meta_title
    if not title and filename:
        stem = re.sub(r'\.pdf$', '', Path(filename).name, flags=re.I)
        stem = ' '.join(re.sub(r'[_]+', ' ', stem).split())
        if re.search(r'[^\W\d_]{3}', stem) and not _ARXIV_STAMP_RE.search('arXiv:'+stem): title = stem[:300]
    return {
        'title': _readable(title),
        'authors': _authors(_metadata_text(reader, '/Author')),
        'abstract': _abstract(text+'\n'+second),
        'year': _year(reader, arxiv_id),
        'arxiv_id': arxiv_id, 'arxiv_version': arxiv_version,
        'doi': meta_doi or text_doi, 'doi_from_metadata': bool(meta_doi),
        'pages': len(reader.pages),
    }


def token(data):
    return hashlib.sha256(data).hexdigest()


def _staged(settings, upload):
    return settings.cache_dir/'uploads'/(upload+'.pdf')


def _kept(settings, upload):
    return settings.cache_dir/'uploaded'/(upload+'.pdf')


def stage(settings, db, data):
    """Hold an upload until it is added, within the PDF cache quota."""
    upload = token(data)
    staging = _staged(settings, upload).parent; kept = _kept(settings, upload).parent
    staging.mkdir(parents=True, exist_ok=True); kept.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for old in staging.glob('*.pdf'):
        try:
            if now-old.stat().st_mtime > 86400: old.unlink(missing_ok=True)
        except FileNotFoundError: pass
    referenced = {r[0] for r in db.execute("SELECT local_path FROM artifacts WHERE local_path IS NOT NULL")}
    for old in kept.glob('*.pdf'):
        try:
            if str(old) not in referenced and now-old.stat().st_mtime > 3600: old.unlink(missing_ok=True)
        except FileNotFoundError: pass
    used = sum(p.stat().st_size for folder in (settings.cache_dir, staging, kept) for p in folder.glob('*.pdf'))
    target = _staged(settings, upload)
    if not target.exists() and not _kept(settings, upload).exists():
        if used+len(data) > settings.cache_max_bytes:
            raise PdfImportError('PDF cache quota reached; increase CORPUS_CACHE_MAX_BYTES or remove cached artifacts')
        part = target.with_suffix('.part')
        part.write_bytes(data); os.replace(part, target)
    else:
        os.utime(target if target.exists() else _kept(settings, upload))
    return upload


def attach(settings, db, paper_id, upload):
    """Keep an uploaded PDF as the paper's cached copy, unless it already has one."""
    if not isinstance(upload, str) or not _TOKEN_RE.fullmatch(upload): raise ValueError('Invalid PDF upload')
    url = 'upload:'+upload
    if db.execute("SELECT 1 FROM artifacts WHERE paper_id=? AND kind='pdf' AND (url=? OR fetch_status='cached')", (paper_id, url)).fetchone():
        return 'kept-existing'
    staged, kept = _staged(settings, upload), _kept(settings, upload)
    if not kept.exists():
        if not staged.exists(): return 'expired'
        os.replace(staged, kept)
    pages = page_texts(kept.read_bytes())
    artifact = db.execute("INSERT INTO artifacts(paper_id,kind,url,local_path,fetched_at,fetch_status) VALUES(?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),'cached')",
                          (paper_id, 'pdf', url, str(kept))).lastrowid
    db.executemany('INSERT INTO pdf_text(paper_id,artifact_id,page,text) VALUES(?,?,?,?)', [(paper_id, artifact, n, text) for n, text in enumerate(pages, 1)])
    # The upload is the reader's copy; other PDF links stay available to cache by hand.
    db.execute("UPDATE artifacts SET fetch_status='linked' WHERE paper_id=? AND kind='pdf' AND id!=? AND fetch_status='pending'", (paper_id, artifact))
    return 'attached'


def from_pdf(found):
    return {'title': found['title'], 'authors': found['authors'], 'abstract': found['abstract'], 'year': found['year'],
            'venue': '', 'doi': None, 'arxiv_id': None, 'openreview_id': None, 'canonical_url': '', 'artifacts': []}


def identify(settings, db, catalog, found, library_paper, threshold):
    """Find the paper this PDF is: in the library, then on arXiv/Crossref. Returns (paper, source, via, notes)."""
    from .catalog import CatalogBusy
    from .db import duplicate_candidates
    notes = []
    candidates = []
    if found['arxiv_id']: candidates.append(('arxiv_id', found['arxiv_id'], found['arxiv_id']+found['arxiv_version'], 'arXiv id', True))
    if found['doi']: candidates.append(('doi', found['doi'], found['doi'], 'DOI', found['doi_from_metadata'] or not found['title']))
    for key, value, query, via, trusted in candidates:
        row = db.execute('SELECT id FROM papers WHERE '+key+'=?', (value,)).fetchone()
        if row:
            paper = library_paper(db, row['id'])
            if trusted or title_similarity(paper['title'], found['title']) >= TITLE_CONFIRMS:
                return paper, 'library', via, notes
            continue
        try:
            result = catalog.resolve([query])[0]
            if result.get('retry_after'):  # arXiv asks for a pause between requests
                time.sleep(result['retry_after']+.1)
                result = catalog.resolve([query])[0]
        except Exception as error:
            notes.append(f'{via} lookup failed: {str(error)[:200]}'); continue
        paper = result.get('paper')
        if not paper:
            if result.get('error'): notes.append(f'{via} lookup: {result["error"][:200]}')
            continue
        if trusted or title_similarity(paper.get('title'), found['title']) >= TITLE_CONFIRMS:
            return paper, 'arxiv' if key == 'arxiv_id' else 'crossref', via, notes
    title = found['title']
    if not title:
        return None, 'pdf', None, notes
    near = duplicate_candidates(db, {'title': title, 'authors': found['authors']})
    if near and near[0]['similarity'] >= threshold:
        return library_paper(db, near[0]['id']), 'library', 'title', notes
    best = (0., None, None)
    for source in ('arxiv', 'crossref'):
        for attempt in range(2):
            try:
                papers, _ = catalog.search(title[:500], source, 'title', 0)
                break
            except CatalogBusy:
                if attempt: papers = []; notes.append(f'{"arXiv" if source == "arxiv" else "Crossref"} was busy')
                else: time.sleep(3.1)
            except Exception as error:
                papers = []; notes.append(f'{"arXiv" if source == "arxiv" else "Crossref"} search failed: {str(error)[:200]}'); break
        for paper in papers:
            score = title_similarity(paper.get('title'), title)
            if score > best[0]: best = (score, paper, source)
    if best[0] >= TITLE_MATCH:
        return best[1], best[2], 'title', notes
    return None, 'pdf', None, notes
