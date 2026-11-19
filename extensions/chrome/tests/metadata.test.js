import { test } from 'node:test';
import assert from 'node:assert/strict';
import '../lib/metadata.js';

const { extractFromMetas, extract } = globalThis.CorpusMeta;

test('reads citation_* tags', () => {
  const m = extractFromMetas([
    { name: 'citation_title', content: '  A   Study of Things ' },
    { name: 'citation_author', content: 'Ada Lovelace' },
    { name: 'citation_author', content: 'Alan Turing' },
    { name: 'citation_doi', content: 'doi:10.5555/example.123' },
    { name: 'citation_publication_date', content: '2023/04/02' },
    { name: 'citation_journal_title', content: 'Journal of Examples' },
  ], { url: 'https://publisher.example/a/1', title: 'ignored' });
  assert.equal(m.title, 'A Study of Things');
  assert.deepEqual(m.authors, ['Ada Lovelace', 'Alan Turing']);
  assert.equal(m.doi, '10.5555/example.123');
  assert.equal(m.year, 2023);
  assert.equal(m.venue, 'Journal of Examples');
  assert.equal(m.hasCitationData, true);
});

test('falls back to the document title and finds a DOI in the URL', () => {
  const m = extractFromMetas([], { url: 'https://doi.org/10.5555/abc.def?x=1', title: 'Some page' });
  assert.equal(m.title, 'Some page');
  assert.equal(m.doi, '10.5555/abc.def');
  assert.equal(m.hasCitationData, false);
});

test('detects arXiv ids from the URL', () => {
  assert.equal(extractFromMetas([], { url: 'https://arxiv.org/abs/2401.01234v2', title: '' }).arxivId, '2401.01234');
  assert.equal(extractFromMetas([], { url: 'https://arxiv.org/pdf/2401.01234', title: '' }).arxivId, '2401.01234');
});

test('ignores empty values and tolerates missing input', () => {
  const m = extractFromMetas([{ name: 'citation_title', content: '' }, { name: '', content: 'x' }], {});
  assert.equal(m.title, '');
  assert.deepEqual(m.authors, []);
  assert.equal(m.year, null);
});

test('extract() reads name and property attributes from a document', () => {
  const el = (attrs) => ({ getAttribute: (k) => attrs[k] ?? null });
  const doc = {
    title: 'Page',
    location: { href: 'https://publisher.example/p' },
    querySelectorAll: () => [el({ name: 'citation_title', content: 'T' }), el({ property: 'og:description', content: 'D' })],
  };
  const m = extract(doc);
  assert.equal(m.title, 'T');
  assert.equal(m.abstract, 'D');
  assert.equal(m.url, 'https://publisher.example/p');
});
