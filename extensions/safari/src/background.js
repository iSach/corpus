// Non-persistent background script: wakes for a message, does one fetch, sleeps.
// The fetch lives here (not in the popup) so closing the popup cannot cancel it.
async function save(meta) {
  const { baseUrl, token, topic } = await browser.storage.local.get(['baseUrl', 'token', 'topic']);
  if (!token) throw new Error('Set your ingest token in the extension options.');
  const base = CorpusPayload.normalizeBase(baseUrl);
  const body = CorpusPayload.buildPayload(meta, { topic });
  let res;
  try {
    res = await fetch(base + '/api/ingest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
      body: JSON.stringify(body),
    });
  } catch (e) {
    throw new Error('Could not reach Corpus. Check the URL and that Safari has granted this extension access to that site.');
  }
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error page */ }
  if (res.status === 401) throw new Error('Corpus rejected the ingest token.');
  if (res.status === 429) throw new Error('Corpus rate limit reached; retry in a minute.');
  if (!res.ok) throw new Error('Corpus returned HTTP ' + res.status + (data && typeof data.detail === 'string' ? ': ' + data.detail : '.'));
  return CorpusPayload.describeResult(data);
}

browser.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === 'save') {
    return save(msg.meta).catch((e) => ({ ok: false, message: e.message }));
  }
});
