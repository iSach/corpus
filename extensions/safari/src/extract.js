// Page metadata extraction. Injected into the active tab on demand and also
// imported by the unit tests, so it only touches the document it is given.
(function (root) {
  const DOI_RE = /\b(10\.\d{4,9}\/[^\s"'<>]+)/i;

  function metaValues(doc, names) {
    const wanted = names.map((n) => n.toLowerCase());
    const out = {};
    for (const el of doc.querySelectorAll('meta[name], meta[property]')) {
      const key = (el.getAttribute('name') || el.getAttribute('property') || '').toLowerCase();
      if (!wanted.includes(key)) continue;
      const value = (el.getAttribute('content') || '').trim();
      if (value) (out[key] = out[key] || []).push(value);
    }
    return out;
  }

  function cleanDoi(text) {
    if (!text) return '';
    const m = String(text).match(DOI_RE);
    return m ? m[1].replace(/[.,;)\]]+$/, '') : '';
  }

  // "Last, First" -> "First Last"; leaves other forms alone.
  function personName(raw) {
    const s = raw.trim().replace(/\s+/g, ' ');
    const parts = s.split(',');
    return parts.length === 2 && parts[0] && parts[1] ? parts[1].trim() + ' ' + parts[0].trim() : s;
  }

  function year(text) {
    const m = String(text || '').match(/\b(1[0-9]{3}|2[01][0-9]{2})\b/);
    return m ? Number(m[1]) : null;
  }

  function jsonLdDoi(doc) {
    for (const el of doc.querySelectorAll('script[type="application/ld+json"]')) {
      const found = cleanDoi(el.textContent);
      if (found) return found;
    }
    return '';
  }

  function fromDocument(doc, pageUrl) {
    const m = metaValues(doc, [
      'citation_title', 'citation_author', 'citation_doi', 'citation_publication_date',
      'citation_date', 'citation_online_date', 'citation_journal_title',
      'citation_conference_title', 'citation_abstract', 'citation_pdf_url',
      'citation_arxiv_id', 'dc.identifier', 'dc.title', 'dc.creator', 'dc.date',
      'og:title', 'description', 'og:description',
    ]);
    const first = (k) => (m[k] && m[k][0]) || '';
    const doi = cleanDoi(first('citation_doi')) || cleanDoi(first('dc.identifier')) ||
      jsonLdDoi(doc) || cleanDoi(pageUrl);
    const authors = (m.citation_author || m['dc.creator'] || []).map(personName);
    const title = first('citation_title') || first('dc.title') || first('og:title') ||
      (doc.title || '').trim();
    return {
      title,
      authors,
      doi,
      arxiv_id: first('citation_arxiv_id'),
      venue: first('citation_journal_title') || first('citation_conference_title'),
      year: year(first('citation_publication_date') || first('citation_date') ||
        first('citation_online_date') || first('dc.date')),
      abstract: first('citation_abstract') || first('description') || first('og:description'),
      pdf_url: first('citation_pdf_url'),
      url: pageUrl || '',
      has_citation_meta: Boolean(m.citation_title),
    };
  }

  const api = { fromDocument, cleanDoi, personName };
  root.CorpusExtract = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : self);
