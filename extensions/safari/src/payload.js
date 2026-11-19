// Builds the /api/ingest request. The server normalizes DOI / arXiv ids and
// does the duplicate check; this only shapes what the page offered.
(function (root) {
  const AGENT = 'corpus-safari-extension/0.1';

  function normalizeBase(raw) {
    const text = String(raw || '').trim();
    if (!text) throw new Error('Set your Corpus URL in the extension options.');
    let u;
    try { u = new URL(text); } catch { throw new Error('The Corpus URL is not valid.'); }
    if (u.protocol !== 'https:' && u.protocol !== 'http:') throw new Error('The Corpus URL must be http(s).');
    if (u.username || u.password) throw new Error('Do not put credentials in the Corpus URL.');
    return u.origin + u.pathname.replace(/\/+$/, '');
  }

  function buildPayload(meta, opts) {
    opts = opts || {};
    const title = String(meta.title || '').trim();
    if (!title) throw new Error('No title found on this page.');
    const paper = { title, authors: meta.authors || [] };
    if (meta.venue) paper.venue = meta.venue;
    if (Number.isInteger(meta.year)) paper.year = meta.year;
    if (meta.abstract) paper.abstract = meta.abstract.slice(0, 100000);
    if (meta.doi) paper.doi = meta.doi;
    if (meta.arxiv_id) paper.arxiv_id = meta.arxiv_id;
    if (meta.url) paper.canonical_url = meta.url;
    if (meta.pdf_url) paper.artifacts = [{ kind: 'pdf', url: meta.pdf_url }];
    if (opts.topic) { paper.field = opts.topic; }
    return {
      agent: AGENT,
      query: 'Saved from browser: ' + (meta.url || '').slice(0, 500),
      papers: [paper],
    };
  }

  function describeResult(body) {
    const r = body && body.results && body.results[0];
    if (!r) return { ok: false, message: 'Unexpected response from Corpus.' };
    if (r.status === 'accept') return { ok: true, message: 'Queued in your Inbox.' };
    if (r.status === 'duplicate') return { ok: true, message: 'Already in Corpus (' + (r.admission || 'known') + ').' };
    return { ok: false, message: 'Rejected: ' + (r.reason || 'unknown reason') };
  }

  const api = { buildPayload, normalizeBase, describeResult, AGENT };
  root.CorpusPayload = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : self);
