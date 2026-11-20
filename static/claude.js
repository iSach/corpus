/* The Claude page: review status, token usage and, for the administrator, every Claude setting. */
window.CorpusClaude = function ({ h, api, render, toast, session, config }) {
  const state = { status: null, error: '', loading: false, busy: false, services: null, secrets: {}, options: {}, servicesError: '' };
  const MODELS = ['claude-sonnet-5-5', 'claude-opus-5-5', 'claude-haiku-5-5', 'claude-fable-5-2'];
  const EFFORTS = [['low', 'Low: quick, fewest tokens'], ['medium', 'Medium'], ['high', 'High'], ['xhigh', 'Extra high'], ['max', 'Max: slowest, most tokens']];
  const BACKENDS = { cli: 'your Claude subscription', api: 'the Anthropic API key' };
  const errorText = error => error.message || String(error);
  const isAdmin = () => { const s = session(); return Boolean(s && s.user && s.user.role === 'admin'); };
  const enabled = () => Boolean(config().ai_review && config().ai_review.enabled);
  const clock = value => new Date(value * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const when = value => { const date = new Date(value * 1000); return date.toDateString() === new Date().toDateString() ? clock(value) : date.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + clock(value); };
  const tokens = value => value >= 1e6 ? `${(value / 1e6).toFixed(1)}M` : value >= 1e3 ? `${Math.round(value / 1e3)}k` : String(value || 0);
  const dollars = value => value ? `$${value.toFixed(2)}` : '–';
  const notice = value => value ? h('p', { class: 'notice error-text', role: 'alert' }, value) : null;

  // Shown in the nav when Claude needs a look: paused, or reviews that keep failing.
  function attention() {
    const status = state.status;
    if (!status || !status.enabled) return null;
    if (status.pacing && status.pacing.paused_until) return { mark: '‖', label: 'Claude is paused' };
    if (status.failed) return { mark: String(status.failed), label: `${status.failed} Claude reviews failed` };
    return null;
  }
  function summary(status) {
    const pacing = status.pacing || {}, queue = status.queue;
    const waiting = queue.inbox + queue.update + queue.library;
    const minutes = seconds => seconds < 90 ? `${seconds} s` : `${Math.round(seconds / 60)} min`;
    return pacing.paused_until ? `Paused until ${clock(pacing.paused_until)}${pacing.reason ? ` · ${pacing.reason}` : ''}`
      : !waiting ? 'All caught up'
      : pacing.daily_limit && pacing.calls_today >= pacing.daily_limit ? 'Daily limit reached; continues tomorrow'
      : pacing.next_call_in ? `Next review in ${minutes(pacing.next_call_in)}` : 'Reviewing';
  }

  async function refresh(detail = false) {
    if (!enabled() && !isAdmin()) return;
    try { state.status = await api(`/api/ai${detail ? '?detail=true' : ''}`); state.error = ''; }
    catch (error) { state.error = errorText(error); }
    render();
  }
  async function load() {
    state.loading = true; render();
    await Promise.all([refresh(true), isAdmin() ? loadServices() : null]);
    state.loading = false; render();
  }
  async function loadServices() {
    try { showServices(await api('/api/admin/services')); }
    catch (error) { state.servicesError = errorText(error); }
  }
  function showServices(data) {
    state.services = data;
    const value = key => data.settings[key].value;
    state.options = { ai_backend: value('ai_backend'), ai_model: value('ai_model'), ai_effort: value('ai_effort'),
      ai_min_interval: String(Math.round(value('ai_min_interval') / 60 * 10) / 10), ai_daily_limit: String(value('ai_daily_limit')) };
  }
  async function update(body) {
    try { state.status = await api('/api/ai?detail=true', { method: 'POST', body }); }
    catch (error) { toast(errorText(error), true); }
    render();
  }
  async function saveServices(changes, done) {
    if (state.busy) return;
    state.busy = true; state.servicesError = ''; render();
    try {
      showServices(await api('/api/admin/services', { method: 'PUT', body: changes }));
      state.secrets = {};
      toast(done);
      await refresh(true);
    } catch (error) { state.servicesError = errorText(error); }
    finally { state.busy = false; render(); }
  }
  function saveOptions(event) {
    event.preventDefault();
    const minutes = Number(state.options.ai_min_interval), limit = Number(state.options.ai_daily_limit);
    if (!Number.isFinite(minutes) || !Number.isInteger(limit)) { state.servicesError = 'Enter numbers for the pacing settings.'; render(); return; }
    saveServices({ ai_backend: state.options.ai_backend, ai_model: state.options.ai_model.trim(), ai_effort: state.options.ai_effort,
      ai_min_interval: Math.round(minutes * 60), ai_daily_limit: limit }, 'Claude settings saved');
  }

  function renderStatus(status) {
    const pacing = status.pacing || {}, queue = status.queue;
    const fact = (label, value) => [h('dt', null, label), h('dd', null, value)];
    return [
      h('h2', null, 'Status'),
      h('div', { class: `claude-state${pacing.paused_until ? ' paused' : ''}`, role: 'status' },
        h('span', null, summary(status)),
        pacing.paused_until ? h('button', { class: 'btn', type: 'button', onclick: () => update({ resume: true }) }, 'RESUME NOW') : null),
      h('dl', { class: 'claude-facts' },
        fact('reviews with', `${BACKENDS[status.backend] || status.backend} · ${status.model} · ${status.effort} effort`),
        fact('today', `${pacing.calls_today || 0}${pacing.daily_limit ? ` of ${pacing.daily_limit}` : ' (no daily limit)'}`),
        fact('waiting', `${queue.inbox + queue.update + queue.library} (${queue.inbox} inbox · ${queue.update} updates · ${queue.library} library)`),
        status.failed ? fact('failed', `${status.failed} after 3 tries`) : null),
      h('label', { class: 'claude-backfill' },
        h('input', { type: 'checkbox', checked: status.backfill, onchange: event => update({ backfill: event.target.checked }) }),
        h('span', null, 'Review library papers too', h('span', { class: 'soft' }, ' · papers already in your library that were never reviewed. Uses many tokens on a large library.')))];
  }
  function renderUsage(status) {
    const days = status.days || [], recent = status.recent || [];
    const month = status.month;
    return [
      h('h2', null, 'Usage'),
      h('p', null, `${month.reviews} reviews this month · ${tokens(month.input_tokens)} in · ${tokens(month.output_tokens)} out${month.cost_usd ? ` · ≈ ${dollars(month.cost_usd)} at API prices` : ''}`),
      days.length ? h('table', { class: 'data-table claude-table' },
        h('thead', null, h('tr', null, ...['Day', 'Reviews', 'Full text', 'Tokens in', 'Tokens out', '≈ API cost'].map(label => h('th', null, label)))),
        h('tbody', null, ...days.map(day => h('tr', null, h('td', null, day.day), h('td', null, day.reviews), h('td', null, day.full_text),
          h('td', null, tokens(day.input_tokens)), h('td', null, tokens(day.output_tokens)), h('td', null, dollars(day.cost_usd)))))) : h('p', { class: 'soft' }, 'No reviews in the last two weeks.'),
      h('h2', null, 'Latest reviews'),
      recent.length ? h('table', { class: 'data-table claude-table' },
        h('thead', null, h('tr', null, ...['When', 'Paper', 'Read', 'Tokens in / out', ''].map(label => h('th', null, label)))),
        h('tbody', null, ...recent.map(job => h('tr', null, h('td', null, when(job.updated)),
          h('td', { class: 'claude-paper', title: job.title || job.paper_id }, job.title || job.paper_id),
          h('td', null, job.evidence === 'full_text' ? 'full text' : 'abstract'),
          h('td', null, job.status === 'complete' ? `${tokens(job.input_tokens)} / ${tokens(job.output_tokens)}` : '–'),
          h('td', { class: job.status === 'failed' ? 'error-text' : 'soft', title: job.error || '' }, job.status === 'complete' ? '' : job.status))))) : h('p', { class: 'soft' }, 'No reviews yet.')];
  }
  function renderSettings() {
    if (!isAdmin()) return [h('h2', null, 'Settings'), h('p', { class: 'soft' }, 'The administrator manages Claude’s model, effort and pacing.')];
    const data = state.services;
    if (!data) return [h('h2', null, 'Settings'), state.servicesError ? notice(state.servicesError) : h('p', null, 'Loading…')];
    const settings = data.settings, options = state.options;
    const source = item => item.source === 'admin' ? 'Saved on this page' : item.source === 'environment' ? 'Set in the server environment' : 'Not set';
    const secret = (key, label, hint) => h('form', { class: 'service-key', onsubmit: event => { event.preventDefault(); if (state.secrets[key]) saveServices({ [key]: state.secrets[key] }, label + ' saved'); } },
      h('div', { class: 'service-key-head' }, h('label', { for: `claude-${key}` }, label), h('span', { class: 'soft' }, source(settings[key]))),
      h('div', { class: 'service-key-input' },
        h('input', { id: `claude-${key}`, class: 'input', type: 'password', autocomplete: 'off', placeholder: settings[key].set ? 'Enter a new key to replace it' : 'Paste the key', value: state.secrets[key] || '', oninput: event => { state.secrets[key] = event.target.value; } }),
        h('button', { class: 'btn', type: 'submit', disabled: state.busy }, 'SAVE'),
        settings[key].source === 'admin' ? h('button', { class: 'btn', type: 'button', disabled: state.busy, onclick: () => saveServices({ [key]: null }, label + ' removed') }, 'REMOVE') : null),
      hint ? h('p', { class: 'soft' }, hint) : null);
    const option = (key, label, ...inputs) => h('div', { class: 'service-option' }, h('label', { for: `claude-${key}` }, label), ...inputs);
    const select = (key, choices) => h('select', { id: `claude-${key}`, class: 'input', onchange: event => { options[key] = event.target.value; render(); } },
      ...choices.map(([value, label]) => h('option', { value, selected: options[key] === value }, label)));
    const unpaced = Number(options.ai_daily_limit) === 0 && Number(options.ai_min_interval) === 0;
    const keys = ['ai_backend', 'ai_model', 'ai_effort', 'ai_min_interval', 'ai_daily_limit'];
    return [
      h('h2', null, 'Settings'),
      h('p', null, data.owner_backend ? `Your library is reviewed with ${BACKENDS[data.owner_backend]}.` : !data.owner_switch ? 'Claude reviews are off for your library: turn CLAUDE on next to your name on the Admin page.' : settings.ai_backend.value === 'off' ? 'Claude reviews are off: choose what to use below.' : 'Claude reviews are off for your library: add a subscription token or an API key.',
        ' ', data.member_backend ? 'Other people’s libraries use the API key once you turn Claude on for them on the Admin page.' : 'Other people’s libraries need an API key; your subscription only reviews your own library.'),
      h('form', { class: 'passkey-form', onsubmit: saveOptions },
        h('div', { class: 'service-options' },
          option('ai_model', 'MODEL', h('input', { id: 'claude-ai_model', class: 'input', list: 'claude-models', value: options.ai_model, required: true, oninput: event => { options.ai_model = event.target.value; } }),
            h('datalist', { id: 'claude-models' }, ...MODELS.map(value => h('option', { value })))),
          option('ai_effort', 'EFFORT', select('ai_effort', EFFORTS)),
          option('ai_min_interval', 'MINUTES BETWEEN REVIEWS', h('input', { id: 'claude-ai_min_interval', class: 'input', type: 'number', min: 0, max: 1440, step: 0.5, value: options.ai_min_interval, required: true, oninput: event => { options.ai_min_interval = event.target.value; render(); } })),
          option('ai_daily_limit', 'REVIEWS PER DAY (0: NO LIMIT)', h('input', { id: 'claude-ai_daily_limit', class: 'input', type: 'number', min: 0, max: 10000, step: 1, value: options.ai_daily_limit, required: true, oninput: event => { options.ai_daily_limit = event.target.value; render(); } })),
          option('ai_backend', 'USE', select('ai_backend', [['auto', 'API key if set, otherwise subscription'], ['cli', 'Subscription only'], ['api', 'API key only'], ['off', 'Nothing: Claude reviews off']]))),
        h('p', { class: 'soft' }, 'Effort is how long Claude thinks before answering. Lower effort uses fewer tokens; abstracts rarely need more than medium.'),
        unpaced ? h('p', { class: 'notice', role: 'status' }, 'No pacing: Claude reviews one paper after another with no daily cap until the queue is empty.') : null,
        h('div', { class: 'account-actions' },
          h('button', { class: 'btn btn-primary', type: 'submit', disabled: state.busy }, 'SAVE CLAUDE SETTINGS'),
          keys.some(key => settings[key].source === 'admin') ? h('button', { class: 'btn', type: 'button', disabled: state.busy, onclick: () => saveServices(Object.fromEntries(keys.map(key => [key, null])), 'Server defaults restored') }, 'USE SERVER DEFAULTS') : null)),
      h('h2', null, 'Keys'),
      secret('claude_oauth_token', 'CLAUDE SUBSCRIPTION TOKEN', `Run claude setup-token on a computer where you’re signed in to Claude, then paste the token. Corpus runs ${data.claude_bin}.`),
      secret('anthropic_api_key', 'ANTHROPIC API KEY', 'Used instead of the subscription when both are set, unless USE says otherwise.'),
      h('p', { class: 'soft' }, 'Keys are stored on the server outside the database, so nightly backups don’t copy them. They are never shown again after saving.'),
      notice(state.servicesError)];
  }
  function renderPage() {
    const status = state.status;
    return h('main', { class: 'passkeys-layout' }, h('div', { class: 'admin-layout claude-layout' },
      h('header', { class: 'admin-head' },
        h('h1', null, 'Claude'),
        h('p', null, 'Claude reads new papers in the background and drafts a relevance review for each one.'),
        notice(state.error)),
      h('section', { class: 'admin-column', ariaLabel: 'Claude status and usage' },
        status && status.enabled ? [...renderStatus(status), ...renderUsage(status)]
          : status ? [h('h2', null, 'Status'), h('p', null, 'Claude reviews are off for this library.'), ...(status.days ? renderUsage(status) : [])]
          : h('p', null, state.loading ? 'Loading…' : 'Claude reviews are not available.')),
      h('section', { class: 'admin-column', ariaLabel: 'Claude settings' }, ...renderSettings())));
  }
  return { load, refresh, attention, renderPage, available: () => enabled() || isAdmin() };
};
