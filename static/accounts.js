/* Invitations and account management. Tokens stay out of server URL logs. */
window.CorpusAccounts = function ({ h, api, render, toast, session }) {
  const params = new URLSearchParams(window.location.hash.slice(1));
  const token = params.get('invite');
  if (token) window.history.replaceState(null, '', window.location.pathname + window.location.search);
  const state = { token, invitation: null, inviteError: '', password: '', confirm: '', busy: false,
    loading: false, error: '', users: [], invitations: [], emailEnabled: false, origin: '', email: '', link: null,
    currentPassword: '', newPassword: '', accountEmail: '', resendKey: '', mailPending: false,
    ingest: { loaded: false, exists: false, created: null, secret: '', error: '' },
    services: null, secrets: {}, options: {}, servicesError: '' };
  const errorText = error => error.message || String(error);
  const date = value => new Date(value * 1000).toLocaleString();
  function notice(value) { return value ? h('p', { class: 'notice error-text', role: 'alert' }, value) : null; }
  function field(id, label, type, value, update, attrs = {}) {
    return [h('label', { for: id }, label), h('input', { id, class: 'input', type, value, oninput: e => update(e.target.value), ...attrs })];
  }
  async function inspect() {
    if (!token) return;
    try { state.invitation = await api('/api/invitations/inspect', { method: 'POST', body: { token } }); }
    catch (error) { state.inviteError = errorText(error); }
  }
  async function accept(event) {
    event.preventDefault();
    if (state.busy) return;
    if (state.password !== state.confirm) { state.inviteError = 'The passwords do not match.'; render(); return; }
    state.busy = true; state.inviteError = ''; render();
    try {
      await api('/api/invitations/accept', { method: 'POST', body: { token, password: state.password } });
      try { localStorage.setItem('corpus.loginEmail', state.invitation.email); } catch (_) {}
      window.location.replace('/');
    } catch (error) { state.inviteError = errorText(error); state.busy = false; render(); }
  }
  function renderInvitation() {
    const invite = state.invitation;
    return h('main', { class: 'gate' }, h('form', { class: 'gate-form', onsubmit: accept },
      h('div', { class: 'gate-brand' }, 'Corpus'),
      h('h1', null, invite && invite.recovery ? 'Set a new password' : 'Your research library awaits'),
      invite ? h('p', null, `Invited as ${invite.email}. ${invite.recovery ? 'Your library will be kept.' : 'Set a password to create your private library.'}`) : null,
      notice(state.inviteError),
      invite ? field('invite-password', 'PASSWORD', 'password', state.password, value => { state.password = value; }, { required: true, minlength: 12, maxlength: 1000, autocomplete: 'new-password' }) : null,
      invite ? h('p', { class: 'soft' }, 'Use at least 12 characters.') : null,
      invite ? field('invite-confirm', 'CONFIRM PASSWORD', 'password', state.confirm, value => { state.confirm = value; }, { required: true, minlength: 12, maxlength: 1000, autocomplete: 'new-password' }) : null,
      invite ? h('button', { class: 'btn btn-primary', type: 'submit', disabled: state.busy }, state.busy ? 'SETTING UP…' : invite.recovery ? 'SAVE PASSWORD' : 'CREATE MY ACCOUNT') : null,
      h('a', { href: '/', class: 'soft' }, 'Back to sign in')
    ));
  }
  async function load() {
    const user = session() && session().user;
    state.accountEmail = user && user.email || '';
    if (user) loadIngestToken();
    if (!user || user.role !== 'admin') return;
    state.loading = true; state.error = ''; render();
    try {
      const data = await api('/api/admin/accounts');
      state.users = data.users; state.invitations = data.invitations;
      state.emailEnabled = data.email_enabled; state.origin = data.public_origin; state.mailPending = data.mail_setup_pending;
      showServices(await api('/api/admin/services'));
    } catch (error) { state.error = errorText(error); }
    finally { state.loading = false; render(); }
  }
  function showServices(data) {
    state.services = data;
    const value = key => data.settings[key].value;
    state.options = { ai_backend: value('ai_backend'), ai_model: value('ai_model'), ai_min_interval: String(Math.round(value('ai_min_interval') / 60 * 10) / 10), ai_daily_limit: String(value('ai_daily_limit')) };
  }
  async function saveServices(changes, done) {
    if (state.busy) return;
    state.busy = true; state.servicesError = ''; render();
    try {
      showServices(await api('/api/admin/services', { method: 'PUT', body: changes }));
      state.secrets = {};
      toast(done);
    } catch (error) { state.servicesError = errorText(error); }
    finally { state.busy = false; render(); }
  }
  function saveOptions(event) {
    event.preventDefault();
    const minutes = Number(state.options.ai_min_interval), limit = Number(state.options.ai_daily_limit);
    if (!Number.isFinite(minutes) || !Number.isInteger(limit)) { state.servicesError = 'Enter numbers for the pacing settings.'; render(); return; }
    saveServices({ ai_backend: state.options.ai_backend, ai_model: state.options.ai_model.trim(), ai_min_interval: Math.round(minutes * 60), ai_daily_limit: limit }, 'Claude settings saved');
  }
  async function toggleClaude(user) {
    if (state.busy) return;
    state.busy = true; state.error = ''; render();
    try {
      await api(`/api/admin/accounts/${encodeURIComponent(user.id)}/ai`, { method: 'POST', body: { enabled: !user.ai } });
      await load(); toast(user.ai ? 'Claude reviews turned off for this account' : 'Claude reviews turned on for this account');
    } catch (error) { state.error = errorText(error); }
    finally { state.busy = false; render(); }
  }
  async function removeInvitee(user) {
    if (state.busy) return;
    state.busy = true; state.error = ''; render();
    try {
      await api(`/api/admin/accounts/${encodeURIComponent(user.id)}/remove`, { method: 'POST' });
      if (state.link && state.link.email === user.email) state.link = null;
      await load(); toast('Invitation removed');
    } catch (error) { state.error = errorText(error); }
    finally { state.busy = false; render(); }
  }
  function renderServices() {
    const data = state.services;
    if (!data) return null;
    const settings = data.settings;
    const source = item => item.source === 'admin' ? 'Saved on this page' : item.source === 'environment' ? 'Set in the server environment' : 'Not set';
    const backend = { cli: 'your Claude subscription (claude -p)', api: 'the Anthropic API key' };
    const secret = (key, label, hint) => h('form', { class: 'service-key', onsubmit: event => { event.preventDefault(); if (state.secrets[key]) saveServices({ [key]: state.secrets[key] }, label + ' saved'); } },
      h('div', { class: 'service-key-head' }, h('label', { for: `service-${key}` }, label), h('span', { class: 'soft' }, source(settings[key]))),
      h('div', { class: 'service-key-input' },
        h('input', { id: `service-${key}`, class: 'input', type: 'password', autocomplete: 'off', placeholder: settings[key].set ? 'Enter a new key to replace it' : 'Paste the key', value: state.secrets[key] || '', oninput: event => { state.secrets[key] = event.target.value; } }),
        h('button', { class: 'btn', type: 'submit', disabled: state.busy }, 'SAVE'),
        settings[key].source === 'admin' ? h('button', { class: 'btn', type: 'button', disabled: state.busy, onclick: () => saveServices({ [key]: null }, label + ' removed') }, 'REMOVE') : null),
      hint ? h('p', { class: 'soft' }, hint) : null);
    const option = (key, label, input) => h('div', { class: 'service-option' }, h('label', { for: `service-${key}` }, label), input);
    return [
      h('h2', null, 'Claude reviews'),
      h('p', null, data.owner_backend ? `Your library is reviewed with ${backend[data.owner_backend]}.` : !data.owner_switch ? 'Claude reviews are off for your library: turn CLAUDE on next to your name under People.' : settings.ai_backend.value === 'off' ? 'Claude reviews are off: choose what to use below.' : 'Claude reviews are off for your library: add a subscription token or an API key.',
        ' ', data.member_backend ? 'Other people’s libraries can use the API key once you turn Claude on for them below.' : 'Other people’s libraries need an API key; your subscription only reviews your own library.'),
      secret('claude_oauth_token', 'CLAUDE SUBSCRIPTION TOKEN', `Run claude setup-token on a computer where you’re signed in to Claude, then paste the token. Corpus runs ${data.claude_bin}.`),
      secret('anthropic_api_key', 'ANTHROPIC API KEY', 'Used instead of the subscription when both are set, unless you prefer the subscription below.'),
      h('form', { class: 'passkey-form', onsubmit: saveOptions },
        h('div', { class: 'service-options' },
        option('ai_backend', 'USE', h('select', { id: 'service-ai_backend', class: 'input', onchange: event => { state.options.ai_backend = event.target.value; } },
          ...[['auto', 'API key if set, otherwise subscription'], ['cli', 'Subscription only'], ['api', 'API key only'], ['off', 'Nothing: Claude reviews off']].map(([value, label]) => h('option', { value, selected: state.options.ai_backend === value }, label)))),
        option('ai_model', 'MODEL', h('input', { id: 'service-ai_model', class: 'input', value: state.options.ai_model, required: true, oninput: event => { state.options.ai_model = event.target.value; } })),
        option('ai_min_interval', 'MINUTES BETWEEN REVIEWS', h('input', { id: 'service-ai_min_interval', class: 'input', type: 'number', min: 0, max: 1440, step: 0.5, value: state.options.ai_min_interval, required: true, oninput: event => { state.options.ai_min_interval = event.target.value; } })),
        option('ai_daily_limit', 'REVIEWS PER DAY (0: NO LIMIT)', h('input', { id: 'service-ai_daily_limit', class: 'input', type: 'number', min: 0, max: 10000, step: 1, value: state.options.ai_daily_limit, required: true, oninput: event => { state.options.ai_daily_limit = event.target.value; } }))),
        h('div', { class: 'account-actions' },
          h('button', { class: 'btn btn-primary', type: 'submit', disabled: state.busy }, 'SAVE CLAUDE SETTINGS'),
          ['ai_backend', 'ai_model', 'ai_min_interval', 'ai_daily_limit'].some(key => settings[key].source === 'admin') ? h('button', { class: 'btn', type: 'button', disabled: state.busy, onclick: () => saveServices({ ai_backend: null, ai_model: null, ai_min_interval: null, ai_daily_limit: null }, 'Server defaults restored') }, 'USE SERVER DEFAULTS') : null)),
      h('h2', null, 'Paper metadata keys'),
      secret('s2_api_key', 'SEMANTIC SCHOLAR API KEY', 'Finds published venues for ICLR, ICML and AISTATS preprints.'),
      secret('openalex_api_key', 'OPENALEX API KEY', 'Optional; OpenAlex also works without a key.'),
      h('p', { class: 'soft' }, 'Keys are stored on the server outside the database, so nightly backups don’t copy them. They are never shown again after saving.'),
      notice(state.servicesError)];
  }
  async function connectResend(event) {
    event.preventDefault();
    if (state.busy) return;
    state.busy = true; state.error = '';
    const key = state.resendKey;
    state.resendKey = ''; render();
    try {
      await api('/api/admin/mail/resend', { method: 'POST', body: { api_key: key } });
      state.mailPending = true;
      toast('Key saved securely. Return to the chat to finish connecting email.');
    } catch (error) { state.error = errorText(error); }
    finally { state.busy = false; render(); }
  }
  async function invite(delivery, email = state.email) {
    if (state.busy) return;
    state.busy = true; state.error = ''; state.link = null; render();
    try {
      const data = await api('/api/admin/invitations', { method: 'POST', body: { email, delivery } });
      state.link = data.url ? data : null;
      state.email = '';
      await load();
      toast(delivery === 'email' ? 'Invitation sent' : 'Invitation link created');
    } catch (error) { state.error = errorText(error); }
    finally { state.busy = false; render(); }
  }
  async function revoke(id) {
    if (state.busy) return;
    state.busy = true; state.error = ''; render();
    try {
      const result = await api(`/api/admin/invitations/${encodeURIComponent(id)}/revoke`, { method: 'POST' });
      if (state.link && state.link.id === id) state.link = null;
      await load(); toast(result.removed ? 'Invitation revoked and removed' : 'Invitation revoked');
    } catch (error) { state.error = errorText(error); }
    finally { state.busy = false; render(); }
  }
  async function status(user) {
    const next = user.status === 'disabled' ? 'active' : 'disabled';
    if (state.busy) return;
    state.busy = true; state.error = ''; render();
    try {
      await api(`/api/admin/accounts/${encodeURIComponent(user.id)}/status`, { method: 'POST', body: { status: next } });
      await load(); toast(next === 'active' ? 'Account enabled' : 'Account disabled and signed out');
    } catch (error) { state.error = errorText(error); }
    finally { state.busy = false; render(); }
  }
  async function loadIngestToken() {
    try { Object.assign(state.ingest, await api('/api/account/ingest-token'), { loaded: true, error: '' }); }
    catch (error) { state.ingest.error = errorText(error); }
    render();
  }
  async function changeIngestToken(method) {
    if (state.busy) return;
    if (method === 'DELETE' && !window.confirm('Revoke this token? The browser extension will stop saving until you generate a new one.')) return;
    state.busy = true; state.ingest.error = ''; render();
    try {
      const data = await api('/api/account/ingest-token', { method });
      state.ingest.secret = data.token || '';
      Object.assign(state.ingest, { exists: data.exists, created: data.created, loaded: true });
    } catch (error) { state.ingest.error = errorText(error); }
    finally { state.busy = false; render(); }
  }
  async function copyIngestToken() {
    try { await navigator.clipboard.writeText(state.ingest.secret); toast('Token copied'); }
    catch (_) { toast('Copy failed; select the token and copy it by hand'); }
  }
  function renderIngestToken() {
    const t = state.ingest;
    return h('section', { class: 'account-section' }, h('h2', null, 'Browser extension'),
      h('p', { class: 'soft' }, 'A personal ingest token lets the Corpus browser extension save papers into your own Inbox. Treat it like a password. It only adds papers for your approval and cannot read your library.'),
      t.secret ? h('div', null,
        h('p', { class: 'soft' }, 'Copy this token now. It is shown only once.'),
        h('input', { class: 'input', id: 'ingest-token-value', type: 'text', readonly: true, value: t.secret, onfocus: e => e.target.select() }),
        h('button', { class: 'btn', type: 'button', onclick: copyIngestToken }, 'COPY')) : null,
      !t.secret && t.exists ? h('p', { class: 'soft' }, `A token was created ${date(t.created)}.`) : null,
      notice(t.error),
      h('div', { class: 'account-actions' },
        h('button', { class: 'btn', type: 'button', disabled: state.busy || !t.loaded, onclick: () => changeIngestToken('POST') }, t.exists ? 'REPLACE TOKEN' : 'GENERATE TOKEN'),
        t.exists ? h('button', { class: 'btn', type: 'button', disabled: state.busy, onclick: () => changeIngestToken('DELETE') }, 'REVOKE') : null));
  }
  async function saveAccount(event) {
    event.preventDefault();
    if (state.busy) return;
    state.busy = true; state.error = ''; render();
    const body = { current_password: state.currentPassword, email: state.accountEmail };
    if (state.newPassword) body.password = state.newPassword;
    try {
      await api('/api/account', { method: 'POST', body });
      try { localStorage.setItem('corpus.loginEmail', state.accountEmail); } catch (_) {}
      window.location.reload();
    } catch (error) { state.error = errorText(error); state.busy = false; render(); }
  }
  function renderAccount(appearance, passkeys) {
    return h('main', { class: 'passkeys-layout' }, h('section', { class: 'passkeys-panel' },
      h('h1', null, 'Your account'), h('p', null, 'Your papers, notes, projects, and PDFs are private to your account.'),
      h('form', { class: 'passkey-form', onsubmit: saveAccount },
        field('account-email', 'EMAIL', 'email', state.accountEmail, value => { state.accountEmail = value; }, { required: true, autocomplete: 'email' }),
        field('account-current', 'CURRENT PASSWORD', 'password', state.currentPassword, value => { state.currentPassword = value; }, { required: true, autocomplete: 'current-password' }),
        field('account-new', 'NEW PASSWORD (OPTIONAL)', 'password', state.newPassword, value => { state.newPassword = value; }, { minlength: 12, maxlength: 1000, autocomplete: 'new-password' }),
        h('p', { class: 'soft' }, 'New passwords need at least 12 characters. Changing your password signs out your other sessions.'),
        notice(state.error), h('button', { class: 'btn btn-primary', type: 'submit', disabled: state.busy }, 'SAVE ACCOUNT')
      ), renderIngestToken(), appearance, passkeys));
  }
  function renderAdmin() {
    if (!session() || !session().user || session().user.role !== 'admin') {
      return h('main', { class: 'passkeys-panel' }, h('h1', null, 'Administrator access required'));
    }
    const link = state.link;
    const mailBody = link ? `I'd like you to try Corpus, a research library for collecting and reviewing papers.\n\nSet up your private account here:\n${link.url}\n\nThe link expires in 72 hours. I'd love to hear what you think!` : '';
    // Two columns on wide screens: people and invitations on the left, the
    // server-wide services on the right. They stack below 900px.
    return h('main', { class: 'passkeys-layout' }, h('div', { class: 'admin-layout' },
      h('header', { class: 'admin-head' },
        h('h1', null, 'People & services'),
        h('p', null, 'Invite friends to try Corpus. Each person gets a private library. You manage invitations and account access.'),
        !state.origin ? h('p', { class: 'notice' }, 'Set the public Corpus address on the server before inviting friends.') : null),
      h('section', { class: 'admin-column', ariaLabel: 'People' },
        h('h2', null, 'Invite someone'),
        !state.emailEnabled ? h('p', { class: 'notice' }, 'Automatic email sending is not connected. Create a link and send it from your usual email app.') : null,
        h('form', { class: 'passkey-form admin-invite', onsubmit: event => { event.preventDefault(); invite('link'); } },
          h('div', { class: 'admin-invite-field' }, ...field('invitation-email', 'FRIEND’S EMAIL', 'email', state.email, value => { state.email = value; }, { required: true, autocomplete: 'off', maxlength: 254 })),
          h('div', { class: 'account-actions' },
            h('button', { class: 'btn btn-primary', type: 'submit', disabled: state.busy }, 'CREATE INVITATION LINK'),
            state.emailEnabled ? h('button', { class: 'btn', type: 'button', disabled: state.busy, onclick: event => { if (event.target.closest('form').reportValidity()) invite('email'); } }, 'SEND EMAIL') : null)),
        h('p', { class: 'soft' }, 'Links expire in 72 hours and work once. A new invitation replaces any previous link for that person.'),
        notice(state.error),
        link ? h('section', { class: 'invitation-result', role: 'status' },
          h('strong', null, `Invitation for ${link.email}`),
          h('p', { class: 'soft' }, 'Copy this link now. It is only shown here when created.'),
          h('input', { class: 'input', ariaLabel: 'Invitation link', value: link.url, readonly: true, onclick: event => event.target.select() }),
          h('div', { class: 'account-actions' },
            h('button', { class: 'btn', type: 'button', onclick: async () => { try { await navigator.clipboard.writeText(link.url); toast('Invitation link copied'); } catch (_) { toast('Select the link and copy it.', true); } } }, 'COPY LINK'),
            h('a', { class: 'btn', href: `mailto:${encodeURIComponent(link.email)}?subject=${encodeURIComponent('Try Corpus')}&body=${encodeURIComponent(mailBody)}` }, 'OPEN IN EMAIL APP'))) : null,
        h('h2', null, 'People'),
        state.loading ? h('p', null, 'Loading…') : null,
        ...state.users.map(user => h('div', { class: 'account-row' },
          h('div', null, h('strong', null, user.email || 'You'), h('div', { class: 'soft' }, user.role === 'admin' ? 'Administrator' : user.status === 'invited' ? 'Invited · waiting for setup' : user.status === 'disabled' ? 'Disabled' : 'Active')),
          h('div', { class: 'account-actions' },
            user.status !== 'invited' ? h('button', { class: 'btn', type: 'button', disabled: state.busy, 'aria-pressed': String(user.ai), title: user.role === 'admin' ? 'Claude reviews for your library' : 'Claude reviews for this library, with the API key', onclick: () => toggleClaude(user) }, user.ai ? 'CLAUDE: ON' : 'CLAUDE: OFF') : null,
            user.role !== 'admin' ? h('button', { class: 'btn', type: 'button', disabled: state.busy || user.status === 'disabled', onclick: () => invite('link', user.email) }, user.status === 'invited' ? 'NEW INVITATION' : 'PASSWORD RESET LINK') : null,
            user.role !== 'admin' && user.status === 'invited' ? h('button', { class: 'btn', type: 'button', disabled: state.busy, onclick: () => removeInvitee(user) }, 'REMOVE') : null,
            user.role !== 'admin' && user.status !== 'invited' ? h('button', { class: 'btn', type: 'button', disabled: state.busy, onclick: () => status(user) }, user.status === 'disabled' ? 'ENABLE' : 'DISABLE') : null))),
        h('h2', null, 'Pending links'),
        !state.invitations.length ? h('p', { class: 'soft' }, 'No pending invitations.') : null,
        ...state.invitations.map(item => h('div', { class: 'account-row' },
          h('div', null, item.email, h('div', { class: 'soft' }, `Expires ${date(item.expires)}`)),
          h('button', { class: 'btn', type: 'button', disabled: state.busy, onclick: () => revoke(item.id) }, 'REVOKE')))),
      h('section', { class: 'admin-column', ariaLabel: 'Services' },
        !state.emailEnabled ? [
          h('h2', null, 'Connect invitation email'),
          state.mailPending ? h('p', { role: 'status' }, 'Your Resend key is saved securely. Return to the chat to finish connecting email.') : null,
          h('form', { class: 'service-key', onsubmit: connectResend },
            h('div', { class: 'service-key-head' }, h('label', { for: 'resend-key' }, 'RESEND API KEY')),
            h('div', { class: 'service-key-input' },
              h('input', { id: 'resend-key', class: 'input', type: 'password', value: state.resendKey, oninput: event => { state.resendKey = event.target.value; }, required: true, autocomplete: 'off', maxlength: 300 }),
              h('button', { class: 'btn', type: 'submit', disabled: state.busy }, state.busy ? 'CONNECTING…' : 'SAVE RESEND KEY')),
            h('p', { class: 'soft' }, 'Use a Full access key for setup. Your key will not be shown after saving.'))] : null,
        renderServices() || (state.loading ? h('p', null, 'Loading…') : null))
    ));
  }
  return { hasInvitation: () => Boolean(token), inspect, renderInvitation, renderAccount, renderAdmin, load };
};
