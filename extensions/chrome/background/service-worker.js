// Stateless service worker: reads settings from storage on every request and
// performs the save, so nothing is kept in memory between events. The popup
// asks for the Corpus origin permission (it needs a user gesture); the fetch
// itself runs here and is not subject to CORS thanks to that host permission.
import { savePaper } from '../lib/api.js';

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== 'corpus:save') return false;
  (async () => {
    try {
      const { baseUrl, token } = await chrome.storage.local.get(['baseUrl', 'token']);
      const result = await savePaper({ baseUrl, token, meta: message.meta });
      sendResponse({ ok: true, result });
    } catch (e) {
      sendResponse({ ok: false, error: e && e.message ? e.message : String(e) });
    }
  })();
  return true; // keep the channel open for the async response
});
