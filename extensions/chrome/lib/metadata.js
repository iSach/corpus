/*
 * Paper metadata extraction from a page's <meta> tags (citation_*, Dublin Core,
 * Open Graph). Pure and DOM-light so it is shared by the Chrome and Safari
 * extensions and unit-tested under Node. It only *detects* hints; the Corpus
 * server does the real parsing and normalization.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.CorpusMeta = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const DOI_RE = /\b10\.\d{4,9}\/[^\s"<>?#]+/i;
  const ARXIV_RE = /arxiv\.org\/(?:abs|pdf)\/([a-z\-]+\/\d{7}|\d{4}\.\d{4,5})(v\d+)?/i;

  function clean(value) {
    return typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : '';
  }

  function doiFrom(text) {
    const m = clean(text).match(DOI_RE);
    return m ? m[0].replace(/[.,;)\]]+$/, '') : '';
  }

  function yearFrom(text) {
    const m = clean(text).match(/\b(1[0-9]{3}|2[01][0-9]{2})\b/);
    return m ? Number(m[1]) : null;
  }

  /** metas: [{name, content}] (name is lower-cased by the caller or here). */
  function extractFromMetas(metas, context) {
    const url = (context && context.url) || '';
    const docTitle = (context && context.title) || '';
    const byName = {};
    for (const m of metas || []) {
      const name = clean(m.name).toLowerCase();
      const content = clean(m.content);
      if (!name || !content) continue;
      (byName[name] = byName[name] || []).push(content);
    }
    const first = (...names) => {
      for (const n of names) if (byName[n] && byName[n][0]) return byName[n][0];
      return '';
    };

    const title = first('citation_title', 'dc.title', 'prism.title', 'og:title') || clean(docTitle);
    const authors = (byName['citation_author'] || byName['dc.creator'] || byName['dc.contributor'] || []).slice(0, 200);
    const doi = doiFrom(first('citation_doi', 'dc.identifier', 'prism.doi', 'doi')) || doiFrom(url);
    let arxivId = first('citation_arxiv_id');
    if (!arxivId) {
      const m = url.match(ARXIV_RE);
      if (m) arxivId = m[1];
    }
    const year = yearFrom(first('citation_publication_date', 'citation_date', 'citation_online_date', 'dc.date', 'prism.publicationdate'));
    const venue = first('citation_journal_title', 'citation_conference_title', 'prism.publicationname');
    const abstract = first('citation_abstract', 'dc.description', 'description', 'og:description');
    const pdfUrl = first('citation_pdf_url');
    const hasCitationData = Boolean(byName['citation_title'] || byName['citation_doi'] || byName['dc.title']);

    return { url, title, authors, doi, arxivId, year, venue, abstract, pdfUrl, hasCitationData };
  }

  function extract(doc) {
    const metas = Array.from(doc.querySelectorAll('meta')).map((el) => ({
      name: el.getAttribute('name') || el.getAttribute('property') || '',
      content: el.getAttribute('content') || '',
    }));
    return extractFromMetas(metas, { url: doc.location.href, title: doc.title });
  }

  return { extractFromMetas, extract, doiFrom, yearFrom };
});
