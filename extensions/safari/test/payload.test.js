'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { buildPayload, normalizeBase, describeResult } = require('../src/payload.js');

test('builds an ingest body with only known fields', () => {
  const body = buildPayload({
    title: ' T ', authors: ['A B'], doi: '10.1000/x', year: 2020, venue: 'V',
    url: 'https://example.org/a', pdf_url: 'https://example.org/a.pdf', abstract: '',
  }, { topic: 'unfiled' });
  assert.equal(body.papers.length, 1);
  const p = body.papers[0];
  assert.equal(p.title, 'T');
  assert.equal(p.field, 'unfiled');
  assert.equal(p.canonical_url, 'https://example.org/a');
  assert.deepEqual(p.artifacts, [{ kind: 'pdf', url: 'https://example.org/a.pdf' }]);
  assert.ok(!('abstract' in p));
  assert.ok(!('admission' in p));
});

test('requires a title', () => {
  assert.throws(() => buildPayload({ title: '  ' }), /No title/);
});

test('normalizeBase', () => {
  assert.equal(normalizeBase('https://corpus.example.com/'), 'https://corpus.example.com');
  assert.equal(normalizeBase('http://127.0.0.1:8000/sub/'), 'http://127.0.0.1:8000/sub');
  assert.throws(() => normalizeBase(''), /Set your Corpus URL/);
  assert.throws(() => normalizeBase('ftp://x'), /http/);
  assert.throws(() => normalizeBase('https://u:p@x.com'), /credentials/);
});

test('describeResult', () => {
  assert.equal(describeResult({ results: [{ status: 'accept' }] }).ok, true);
  assert.equal(describeResult({ results: [{ status: 'duplicate', admission: 'pending' }] }).ok, true);
  assert.equal(describeResult({ results: [{ status: 'reject', reason: 'bad' }] }).ok, false);
  assert.equal(describeResult({}).ok, false);
});
