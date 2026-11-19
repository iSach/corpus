import { normalizeBaseUrl } from '../lib/api.js';

const $ = (id) => document.getElementById(id);
const statusEl = $('status');
let meta = null;

function setStatus(text, kind = '') { statusEl.textContent = text; statusEl.className = kind; }

async function detect(tab) {
  if (!tab || !/^https?:/.test(tab.url || '')) throw new Error('Open a paper page first. This page cannot be read.');
  // activeTab grants access for this click; no static content scripts needed.
  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['lib/metadata.js'] });
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: () => globalThis.CorpusMeta.extract(document),
  });
  return result;
}

function show(m) {
  $('paper').hidden = false;
  $('title').textContent = m.title || '(no title found)';
  $('authors').textContent = m.authors.slice(0, 4).join(', ') + (m.authors.length > 4 ? ', …' : '');
  $('doi').textContent = m.doi ? 'DOI ' + m.doi : m.arxivId ? 'arXiv ' + m.arxivId : '';
}

async function ensureOriginPermission(baseUrl) {
  const origins = [normalizeBaseUrl(baseUrl) + '/*'];
  if (await chrome.permissions.contains({ origins })) return;
  // Must run inside the click handler (user gesture).
  if (!(await chrome.permissions.request({ origins }))) {
    throw new Error('Permission to contact your Corpus server was declined.');
  }
}

async function save() {
  $('save').disabled = true;
  setStatus('Saving…');
  try {
    const { baseUrl, token } = await chrome.storage.local.get(['baseUrl', 'token']);
    if (!baseUrl || !token) throw new Error('Set your Corpus address and ingest token in Options first.');
    await ensureOriginPermission(baseUrl);
    const reply = await chrome.runtime.sendMessage({ type: 'corpus:save', meta });
    if (!reply || !reply.ok) throw new Error((reply && reply.error) || 'No response from the extension.');
    setStatus(reply.result.status === 'duplicate' ? 'Already in your library.' : 'Saved. It is waiting in your Inbox.', 'ok');
  } catch (e) {
    setStatus(e.message, 'error');
    $('save').disabled = false;
  }
}

$('settings').addEventListener('click', (e) => { e.preventDefault(); chrome.runtime.openOptionsPage(); });
$('save').addEventListener('click', save);

(async () => {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    meta = await detect(tab);
    show(meta);
    $('save').disabled = false;
    if (!meta.hasCitationData && !meta.doi && !meta.arxivId) setStatus('No paper metadata found; Corpus will try to read the page itself.');
  } catch (e) {
    setStatus(e.message, 'error');
  }
})();
