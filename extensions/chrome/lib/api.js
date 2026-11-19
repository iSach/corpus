/*
 * Corpus API client shared by the Chrome and Safari extensions. Everything the
 * network layer needs is injected (`fetchImpl`), so it is testable under Node.
 * The server (POST /api/ingest, Bearer token) parses the page; hints extracted
 * from the page are only a fallback when the server cannot resolve the link.
 */

export const AGENT = 'browser-extension';

/** Returns the origin ("https://host[:port]") for a base URL, or throws. */
export function normalizeBaseUrl(input) {
  let u;
  try { u = new URL(String(input || '').trim()); } catch { throw new Error('Enter your Corpus address, for example https://corpus.example'); }
  const local = u.hostname === 'localhost' || u.hostname === '127.0.0.1' || u.hostname === '[::1]';
  if (u.protocol !== 'https:' && !(u.protocol === 'http:' && local)) {
    throw new Error('Use an https:// address (plain http is only allowed for localhost)');
  }
  return u.origin;
}

/** The identifier the server should resolve: a DOI beats the page URL. */
export function identifierFor(meta) {
  if (meta.doi) return meta.doi;
  if (meta.arxivId) return 'arxiv:' + meta.arxivId;
  return meta.url;
}

export function buildIdentifierItem(meta) {
  return { identifier: identifierFor(meta), query: meta.url };
}

/** Fallback item built from page hints; used when the server cannot resolve the link. */
export function buildHintItem(meta) {
  if (!meta.title) return null;
  const item = { title: meta.title, query: meta.url };
  if (meta.authors && meta.authors.length) item.authors = meta.authors;
  if (meta.doi) item.doi = meta.doi;
  if (meta.arxivId) item.arxiv_id = meta.arxivId;
  if (meta.year) item.year = meta.year;
  if (meta.venue) item.venue = meta.venue;
  if (meta.abstract) item.abstract = meta.abstract;
  if (/^https?:\/\//.test(meta.url || '')) item.canonical_url = meta.url;
  return item;
}

async function postIngest(fetchImpl, baseUrl, token, item) {
  let response;
  try {
    response = await fetchImpl(baseUrl + '/api/ingest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
      body: JSON.stringify({ agent: AGENT, query: item.query || '', papers: [item] }),
    });
  } catch (e) {
    throw new Error('Could not reach Corpus at ' + baseUrl + '. Check the address and your connection.');
  }
  let data = null;
  try { data = await response.json(); } catch { /* non-JSON error body */ }
  if (response.status === 401) throw new Error('Corpus rejected the ingest token. Check it in the extension options.');
  if (response.status === 429) throw new Error('Corpus is rate limiting saves. Wait a minute and try again.');
  if (!response.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === 'string' ? detail : 'Corpus returned HTTP ' + response.status);
  }
  const result = data && data.results && data.results[0];
  if (!result) throw new Error('Unexpected response from Corpus');
  return result;
}

/**
 * Save the page. Resolves to {status: 'queued'|'duplicate', id, title?} or throws
 * an Error whose message is safe to show the user.
 */
export async function savePaper({ fetchImpl = fetch, baseUrl, token, meta }) {
  const origin = normalizeBaseUrl(baseUrl);
  if (!token) throw new Error('Add your ingest token in the extension options.');
  let result = await postIngest(fetchImpl, origin, token, buildIdentifierItem(meta));
  let reason = result.reason;
  if (result.status === 'reject') {
    const hint = buildHintItem(meta);
    if (hint) {
      result = await postIngest(fetchImpl, origin, token, hint);
      if (result.status === 'reject') reason = result.reason;
    }
  }
  if (result.status === 'accept') return { status: 'queued', id: result.id };
  if (result.status === 'duplicate') return { status: 'duplicate', id: result.id };
  throw new Error(reason || 'Corpus could not read this page as a paper.');
}
