import { test } from 'node:test';
import assert from 'node:assert/strict';
import { normalizeBaseUrl, identifierFor, buildHintItem, savePaper } from '../lib/api.js';

const meta = { url: 'https://publisher.example/a/1', title: 'A Study', authors: ['Ada Lovelace'], doi: '', arxivId: '', year: 2023, venue: '', abstract: '' };

function fakeFetch(...replies) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init, body: JSON.parse(init.body) });
    const r = replies.shift();
    if (r instanceof Error) throw r;
    return { status: r.status ?? 200, ok: (r.status ?? 200) < 400, json: async () => r.json };
  };
  fn.calls = calls;
  return fn;
}
const ingest = (status, extra = {}) => ({ json: { results: [{ index: 0, status, id: 'p1', ...extra }] } });

test('normalizeBaseUrl enforces https except for localhost', () => {
  assert.equal(normalizeBaseUrl('https://corpus.example/some/path'), 'https://corpus.example');
  assert.equal(normalizeBaseUrl('http://localhost:8000/'), 'http://localhost:8000');
  assert.throws(() => normalizeBaseUrl('http://corpus.example'), /https/);
  assert.throws(() => normalizeBaseUrl('not a url'), /address/);
});

test('identifier prefers DOI, then arXiv, then the URL', () => {
  assert.equal(identifierFor({ ...meta, doi: '10.5555/x', arxivId: '2401.01234' }), '10.5555/x');
  assert.equal(identifierFor({ ...meta, arxivId: '2401.01234' }), 'arxiv:2401.01234');
  assert.equal(identifierFor(meta), meta.url);
});

test('hint item is null without a title and otherwise carries the hints', () => {
  assert.equal(buildHintItem({ ...meta, title: '' }), null);
  assert.deepEqual(buildHintItem(meta), { title: 'A Study', query: meta.url, authors: ['Ada Lovelace'], year: 2023, canonical_url: meta.url });
});

test('saves with the Bearer token and reports queued', async () => {
  const f = fakeFetch(ingest('accept'));
  const out = await savePaper({ fetchImpl: f, baseUrl: 'https://corpus.example/', token: 'tok', meta });
  assert.deepEqual(out, { status: 'queued', id: 'p1' });
  assert.equal(f.calls[0].url, 'https://corpus.example/api/ingest');
  assert.equal(f.calls[0].init.headers.Authorization, 'Bearer tok');
  assert.deepEqual(f.calls[0].body.papers, [{ identifier: meta.url, query: meta.url }]);
});

test('reports duplicates', async () => {
  const out = await savePaper({ fetchImpl: fakeFetch(ingest('duplicate')), baseUrl: 'https://corpus.example', token: 't', meta });
  assert.equal(out.status, 'duplicate');
});

test('retries once with page hints when the server cannot resolve the link', async () => {
  const f = fakeFetch(ingest('reject', { reason: 'no citation metadata' }), ingest('accept'));
  const out = await savePaper({ fetchImpl: f, baseUrl: 'https://corpus.example', token: 't', meta });
  assert.equal(out.status, 'queued');
  assert.equal(f.calls.length, 2);
  assert.equal(f.calls[1].body.papers[0].title, 'A Study');
});

test('surfaces the server reason when nothing can be saved', async () => {
  const f = fakeFetch(ingest('reject', { reason: 'no citation metadata' }));
  await assert.rejects(savePaper({ fetchImpl: f, baseUrl: 'https://corpus.example', token: 't', meta: { ...meta, title: '' } }), /no citation metadata/);
});

test('maps auth, rate-limit and network errors to readable messages', async () => {
  const run = (reply) => savePaper({ fetchImpl: fakeFetch(reply), baseUrl: 'https://corpus.example', token: 't', meta });
  await assert.rejects(run({ status: 401, json: { detail: 'x' } }), /ingest token/);
  await assert.rejects(run({ status: 429, json: {} }), /rate limiting/);
  await assert.rejects(run(new Error('boom')), /Could not reach Corpus/);
  await assert.rejects(savePaper({ fetchImpl: fakeFetch(), baseUrl: 'https://corpus.example', token: '', meta }), /token/);
});
