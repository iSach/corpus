const $ = (id) => document.getElementById(id);

browser.storage.local.get(['baseUrl', 'token', 'topic']).then((s) => {
  $('baseUrl').value = s.baseUrl || ''; $('token').value = s.token || ''; $('topic').value = s.topic || '';
});

$('form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const msg = $('msg');
  try {
    const base = CorpusPayload.normalizeBase($('baseUrl').value);
    // Request access to the Corpus origin only (a user gesture is required).
    const granted = await browser.permissions.request({ origins: [new URL(base).origin + '/*'] });
    await browser.storage.local.set({ baseUrl: base, token: $('token').value.trim(), topic: $('topic').value.trim() });
    msg.textContent = granted ? 'Saved.' : 'Saved, but site access was not granted. Allow it in Safari Settings → Extensions → Corpus Saver.';
  } catch (err) {
    msg.textContent = err.message;
  }
});
