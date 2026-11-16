/* Invitations and account management. Tokens stay out of server URL logs. */
window.CorpusAccounts = function ({ h, api, render, toast, session }) {
  const params = new URLSearchParams(window.location.hash.slice(1));
  const token = params.get('invite');
  if (token) window.history.replaceState(null, '', window.location.pathname + window.location.search);
  const state = { token, invitation: null, inviteError: '', password: '', confirm: '', busy: false,
    loading: false, error: '', users: [], invitations: [], emailEnabled: false, origin: '', email: '', link: null,
    currentPassword: '', newPassword: '', accountEmail: '' };
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
    if (!user || user.role !== 'admin') return;
    state.loading = true; state.error = ''; render();
    try {
      const data = await api('/api/admin/accounts');
      state.users = data.users; state.invitations = data.invitations;
      state.emailEnabled = data.email_enabled; state.origin = data.public_origin;
    } catch (error) { state.error = errorText(error); }
    finally { state.loading = false; render(); }
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
      await api(`/api/admin/invitations/${encodeURIComponent(id)}/revoke`, { method: 'POST' });
      if (state.link && state.link.id === id) state.link = null;
      await load(); toast('Invitation revoked');
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
  function renderAccount() {
    return h('main', { class: 'passkeys-layout' }, h('section', { class: 'passkeys-panel' },
      h('h1', null, 'Your account'), h('p', null, 'Your papers, notes, projects, and PDFs are private to your account.'),
      h('form', { class: 'passkey-form', onsubmit: saveAccount },
        field('account-email', 'EMAIL', 'email', state.accountEmail, value => { state.accountEmail = value; }, { required: true, autocomplete: 'email' }),
        field('account-current', 'CURRENT PASSWORD', 'password', state.currentPassword, value => { state.currentPassword = value; }, { required: true, autocomplete: 'current-password' }),
        field('account-new', 'NEW PASSWORD (OPTIONAL)', 'password', state.newPassword, value => { state.newPassword = value; }, { minlength: 12, maxlength: 1000, autocomplete: 'new-password' }),
        h('p', { class: 'soft' }, 'New passwords need at least 12 characters. Changing your password signs out your other sessions.'),
        notice(state.error), h('button', { class: 'btn btn-primary', type: 'submit', disabled: state.busy }, 'SAVE ACCOUNT')
      )));
  }
  function renderAdmin() {
    if (!session() || !session().user || session().user.role !== 'admin') {
      return h('main', { class: 'passkeys-panel' }, h('h1', null, 'Administrator access required'));
    }
    const link = state.link;
    const mailBody = link ? `I'd like you to try Corpus, a research library for collecting and reviewing papers.\n\nSet up your private account here:\n${link.url}\n\nThe link expires in 72 hours. I'd love to hear what you think!` : '';
    return h('main', { class: 'passkeys-layout' }, h('section', { class: 'passkeys-panel admin-panel' },
      h('h1', null, 'People & invitations'),
      h('p', null, 'Invite friends to try Corpus. Each person gets a private library. You manage invitations and account access.'),
      !state.emailEnabled ? h('p', { class: 'notice' }, 'Automatic email sending is not connected. Create a link and send it from your usual email app.') : null,
      !state.origin ? h('p', { class: 'notice' }, 'Set the public Corpus address on the server before inviting friends.') : null,
      h('form', { class: 'passkey-form', onsubmit: event => { event.preventDefault(); invite('link'); } },
        field('invitation-email', 'FRIEND’S EMAIL', 'email', state.email, value => { state.email = value; }, { required: true, autocomplete: 'off', maxlength: 254 }),
        h('p', { class: 'soft' }, 'Links expire in 72 hours and work once. A new invitation replaces any previous link for that person.'),
        h('div', { class: 'account-actions' },
          h('button', { class: 'btn btn-primary', type: 'submit', disabled: state.busy }, 'CREATE INVITATION LINK'),
          state.emailEnabled ? h('button', { class: 'btn', type: 'button', disabled: state.busy, onclick: event => { if (event.target.closest('form').reportValidity()) invite('email'); } }, 'SEND EMAIL') : null)),
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
        user.role !== 'admin' ? h('div', { class: 'account-actions' },
          h('button', { class: 'btn', type: 'button', disabled: state.busy || user.status === 'disabled', onclick: () => invite('link', user.email) }, user.status === 'invited' ? 'NEW INVITATION' : 'PASSWORD RESET LINK'),
          user.status !== 'invited' ? h('button', { class: 'btn', type: 'button', disabled: state.busy, onclick: () => status(user) }, user.status === 'disabled' ? 'ENABLE' : 'DISABLE') : null) : null)),
      h('h2', null, 'Pending links'),
      !state.invitations.length ? h('p', { class: 'soft' }, 'No pending invitations.') : null,
      ...state.invitations.map(item => h('div', { class: 'account-row' },
        h('div', null, item.email, h('div', { class: 'soft' }, `Expires ${date(item.expires)}`)),
        h('button', { class: 'btn', type: 'button', disabled: state.busy, onclick: () => revoke(item.id) }, 'REVOKE')))
    ));
  }
  return { hasInvitation: () => Boolean(token), inspect, renderInvitation, renderAccount, renderAdmin, load };
};
