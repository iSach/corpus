'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { fromDocument, cleanDoi, personName } = require('../src/extract.js');

// Minimal stand-in for the bits of Document that extraction uses.
function fakeDoc({ meta = [], title = '', ld = [] } = {}) {
  const metas = meta.map(([k, v]) => ({
    getAttribute: (a) => (a === 'name' ? k : a === 'content' ? v : null),
  }));
  const scripts = ld.map((t) => ({ textContent: t }));
  return {
    title,
    querySelectorAll: (sel) => (sel.startsWith('meta') ? metas : scripts),
  };
}

test('reads citation_* tags', () => {
  const m = fromDocument(fakeDoc({
    meta: [
      ['citation_title', 'An Example Study'],
      ['citation_author', 'Doe, Jane'],
      ['citation_author', 'John Smith'],
      ['citation_doi', 'doi:10.1000/xyz123'],
      ['citation_publication_date', '2023/05/01'],
      ['citation_journal_title', 'Journal of Examples'],
      ['citation_pdf_url', 'https://example.org/p.pdf'],
    ],
  }), 'https://example.org/article/1');
  assert.equal(m.title, 'An Example Study');
  assert.deepEqual(m.authors, ['Jane Doe', 'John Smith']);
  assert.equal(m.doi, '10.1000/xyz123');
  assert.equal(m.year, 2023);
  assert.equal(m.venue, 'Journal of Examples');
  assert.equal(m.has_citation_meta, true);
});

test('falls back to document title and DOI in the URL', () => {
  const m = fromDocument(fakeDoc({ title: ' Plain Page ' }), 'https://doi.org/10.1000/abc.def');
  assert.equal(m.title, 'Plain Page');
  assert.equal(m.doi, '10.1000/abc.def');
  assert.equal(m.has_citation_meta, false);
  assert.deepEqual(m.authors, []);
});

test('finds DOI in JSON-LD', () => {
  const m = fromDocument(fakeDoc({ title: 'T', ld: ['{"identifier":"https://doi.org/10.5555/ld.1"}'] }), 'https://example.org/');
  assert.equal(m.doi, '10.5555/ld.1');
});

test('cleanDoi and personName edge cases', () => {
  assert.equal(cleanDoi('see 10.1000/abc).'), '10.1000/abc');
  assert.equal(cleanDoi('nothing here'), '');
  assert.equal(personName('Single'), 'Single');
  assert.equal(personName('A, B, C'), 'A, B, C');
});
