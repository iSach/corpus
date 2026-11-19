import { normalizeBaseUrl } from '../lib/api.js';

const $ = (id) => document.getElementById(id);
const statusEl = $('status');
const setStatus = (t, k = '') => { statusEl.textContent = t; statusEl.className = k; };

chrome.storage.local.get(['baseUrl', 'token']).then(({ baseUrl, token }) => {
  $('baseUrl').value = baseUrl || '';
  $('token').value = token || '';
});

$('form').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    const origin = normalizeBaseUrl($('baseUrl').value);
    // Ask for just the Corpus origin (this is a user gesture), not <all_urls>.
    const granted = await chrome.permissions.request({ origins: [origin + '/*'] });
    if (!granted) throw new Error('Permission to contact ' + origin + ' was declined.');
    await chrome.storage.local.set({ baseUrl: origin, token: $('token').value.trim() });
    $('baseUrl').value = origin;
    setStatus('Saved.', 'ok');
  } catch (err) {
    setStatus(err.message, 'error');
  }
});
