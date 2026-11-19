const $ = (id) => document.getElementById(id);
let meta = null;

async function detect() {
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  if (!tab || !/^https?:/.test(tab.url || '')) throw new Error('Open a paper page first.');
  await browser.scripting.executeScript({ target: { tabId: tab.id }, files: ['src/extract.js'] });
  const [res] = await browser.scripting.executeScript({
    target: { tabId: tab.id },
    func: (url) => globalThis.CorpusExtract.fromDocument(document, url),
    args: [tab.url],
  });
  return res.result;
}

function show(m) {
  $('detected').hidden = false;
  $('title').textContent = m.title || '(no title found)';
  $('authors').textContent = m.authors.length ? m.authors.join(', ') : 'No authors found on the page';
  $('doi').textContent = m.doi ? 'DOI ' + m.doi : 'No DOI found';
  $('status').textContent = m.has_citation_meta ? 'Detected paper metadata.' : 'No citation metadata; using page title.';
}

$('save').addEventListener('click', async () => {
  $('save').disabled = true;
  $('result').className = ''; $('result').textContent = 'Saving…';
  const r = await browser.runtime.sendMessage({ type: 'save', meta });
  $('result').className = r.ok ? 'ok' : 'error';
  $('result').textContent = r.message;
  $('save').disabled = r.ok;
});
$('options').addEventListener('click', (e) => { e.preventDefault(); browser.runtime.openOptionsPage(); });

detect().then((m) => { meta = m; show(m); $('save').disabled = !m.title; })
  .catch((e) => { $('status').textContent = e.message; });
