/* Corpus is deliberately a thin client. The server owns search, facets,
   authentication, metadata normalization, export and all persistent state. */
(function () {
  'use strict';

  const root = document.getElementById('app');
  const toastRegion = document.getElementById('toast-region');
  const SCORE_KEYS = ['rel', 'cred', 'qual', 'reimpl'];
  const SCORE_LABELS = { rel: 'relevance', cred: 'credibility', qual: 'quality', reimpl: 'reimpl' };
  const FACET_KEYS = ['field', 'state', 'has', 'venue_class', 'year'];
  const VIEW_NAMES = ['ROWS', 'BARS', 'LEDGER'];
  const VIEW_NOTES = ['numeric scores', 'score bars', 'ledger · zebra rows'];
  const ARTIFACT_KINDS = ['pdf', 'code', 'data', 'slides', 'video', 'other'];

  const persistedView = (() => {
    try { return Math.max(0, Math.min(2, Number(localStorage.getItem('corpus.view') || 0))); }
    catch (_) { return 0; }
  })();

  const state = {
    booting: true,
    bootError: '',
    config: { fields: [], auth_required: false },
    session: null,
    screen: 'library',
    routePaperId: null,
    q: '',
    papers: [],
    total: 0,
    libraryTotal: 0,
    elapsed: null,
    offset: 0,
    limit: 100,
    sort: 'rel',
    resultFacets: {},
    queryError: '',
    selectedId: null,
    selectedDetail: null,
    selectedLoading: false,
    selection: new Set(),
    facetFilters: { field: [], state: [], has: [], venue_class: [], year: [] },
    floors: { rel: null, cred: null, qual: null, reimpl: null },
    view: persistedView,
    showFacets: true,
    showDetail: true,
    showConsole: false,
    sql: { text: '', loading: false, result: null, error: '', smartName: '' },
    stats: null,
    statsLoading: false,
    statsError: '',
    smartLists: [],
    smartListsLoaded: false,
    form: null,
    formLoading: false,
    formError: '',
    duplicateCandidates: [],
    duplicateLoading: false,
    export: { scope: 'query', format: 'bibtex', include: { scores: true, notes: true, code: false, local_paths: false }, preview: '', filename: '', mime: '', count: 0, loading: false, error: '' },
    palette: false,
    paletteFilter: '',
    paletteIndex: 0,
    paletteListName: ''
  };

  let papersRequest = 0;
  let paperRequest = 0;
  let searchTimer = null;
  let exportTimer = null;
  let duplicateTimer = null;
  let statsPromise = null;
  let formPromise = null;

  function h(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) Object.entries(attrs).forEach(([key, value]) => {
      if (value == null || value === false) return;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'html') node.innerHTML = value;
      else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
      else if (key === 'dataset' && typeof value === 'object') Object.entries(value).forEach(([k, v]) => { node.dataset[k] = v; });
      else if (key === 'checked' || key === 'selected' || key === 'multiple' || key === 'disabled' || key === 'hidden' || key === 'required' || key === 'autofocus') node[key] = Boolean(value);
      else if (key === 'value') node.value = value;
      else if (key === 'onclick' || key === 'oninput' || key === 'onchange' || key === 'onkeydown' || key === 'onkeyup' || key === 'onfocus' || key === 'onblur' || key === 'onsubmit' || key === 'onmousedown') node.addEventListener(key.slice(2), value);
      else if (key === 'ariaLabel') node.setAttribute('aria-label', value);
      else if (key === 'ariaCurrent') node.setAttribute('aria-current', value);
      else if (key === 'ariaSelected') node.setAttribute('aria-selected', value);
      else if (key === 'ariaHidden') node.setAttribute('aria-hidden', value);
      else node.setAttribute(key, value === true ? '' : String(value));
    });
    children.flat(Infinity).forEach(child => {
      if (child == null || child === false) return;
      node.appendChild(child.nodeType ? child : document.createTextNode(String(child)));
    });
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function captureRenderState() {
    const active = document.activeElement;
    let focus = null;
    if (active && active !== document.body && active.isConnected) {
      const key = active.dataset.focusKey || active.id || '';
      if (key) {
        focus = { key, start: typeof active.selectionStart === 'number' ? active.selectionStart : null, end: typeof active.selectionEnd === 'number' ? active.selectionEnd : null };
      }
    }
    const scroll = [...document.querySelectorAll('[data-scroll-key]')].map(node => ({ key: node.dataset.scrollKey, top: node.scrollTop, left: node.scrollLeft }));
    return { focus, scroll };
  }
  function restoreRenderState(snapshot) {
    if (!snapshot) return;
    snapshot.scroll.forEach(saved => {
      const node = document.querySelector(`[data-scroll-key="${saved.key}"]`);
      if (node) { node.scrollTop = saved.top; node.scrollLeft = saved.left; }
    });
    if (!snapshot.focus) return;
    const node = document.getElementById(snapshot.focus.key) || document.querySelector(`[data-focus-key="${snapshot.focus.key}"]`);
    if (!node || typeof node.focus !== 'function') return;
    node.focus({ preventScroll: true });
    if (snapshot.focus.start != null && typeof node.setSelectionRange === 'function') {
      try { node.setSelectionRange(snapshot.focus.start, snapshot.focus.end); } catch (_) {}
    }
  }
  function text(value) { return value == null || value === '' ? '' : String(value); }
  function fmt(value, empty = '—') { return value == null || value === '' ? empty : String(value); }
  function number(value) { return value == null || value === '' || Number.isNaN(Number(value)) ? null : Number(value); }
  function score(value) { const n = number(value); return n == null ? null : Math.max(0, Math.min(10, n)); }
  function scoreLabel(value) { const n = score(value); return n == null ? '—' : String(n); }
  function scoreClass(value) { const n = score(value); return n == null ? '' : n >= 8 ? 'high' : n <= 5 ? 'low' : ''; }
  function fieldLabel(id) {
    const found = (state.config.fields || []).find(f => String(f.id) === String(id));
    return found ? found.label : fmt(id);
  }
  function toast(message, isError = false) {
    const node = h('div', { class: `toast${isError ? ' error' : ''}` }, message);
    toastRegion.appendChild(node);
    window.setTimeout(() => node.remove(), isError ? 6500 : 3200);
  }
  function getError(error) {
    if (!error) return 'Request failed';
    if (typeof error === 'string') return error;
    if (error.detail) {
      if (typeof error.detail === 'string') return error.detail;
      if (error.detail.message) return `${error.detail.message}${error.detail.position == null ? '' : ` (at ${error.detail.position})`}`;
      try { return JSON.stringify(error.detail); } catch (_) { return 'Request failed'; }
    }
    return error.message || 'Request failed';
  }

  async function api(path, options = {}) {
    const opts = { credentials: 'same-origin', ...options, headers: { ...(options.headers || {}) } };
    if (options.body && typeof options.body !== 'string') {
      opts.body = JSON.stringify(options.body);
      opts.headers['Content-Type'] = 'application/json';
    }
    const response = await fetch(path, opts);
    let payload = null;
    const type = response.headers.get('content-type') || '';
    try { payload = type.includes('json') ? await response.json() : await response.text(); }
    catch (_) { payload = null; }
    if (!response.ok) {
      const error = new Error(getError(payload) || response.statusText || `HTTP ${response.status}`);
      error.status = response.status;
      error.detail = payload && payload.detail != null ? payload.detail : payload;
      throw error;
    }
    return payload;
  }

  function routeFromLocation() {
    const url = new URL(window.location.href);
    const path = url.pathname.replace(/\/+$/, '') || '/';
    let screen = url.searchParams.get('screen') || '';
    if (!screen) {
      const segment = path.split('/').filter(Boolean)[0];
      screen = ['stats', 'add', 'export', 'library'].includes(segment) ? segment : 'library';
    }
    if (screen === 'paper') screen = 'library';
    if (!['library', 'stats', 'add', 'export'].includes(screen)) screen = 'library';
    return { screen, paperId: url.searchParams.get('paper') || url.searchParams.get('id') || null };
  }

  function routeUrl(screen, paperId) {
    const params = new URLSearchParams();
    if (screen !== 'library') params.set('screen', screen);
    if (paperId) params.set(screen === 'add' ? 'id' : 'paper', paperId);
    const query = params.toString();
    return `${window.location.pathname.replace(/\/[^/]*$/, '/') || '/'}${query ? `?${query}` : ''}`;
  }

  function navigate(screen, paperId = null, replace = false) {
    const url = routeUrl(screen, paperId);
    window.history[replace ? 'replaceState' : 'pushState']({ screen, paperId }, '', url);
    activateRoute({ screen, paperId });
  }

  function activateRoute(route) {
    state.screen = route.screen;
    state.routePaperId = route.paperId;
    state.formError = '';
    if (route.screen === 'library') {
      if (route.paperId) {
        state.selectedId = route.paperId;
        loadPaper(route.paperId);
      }
      if (!state.papers.length && state.session && !state.booting) loadPapers();
    } else if (route.screen === 'stats') loadStats();
    else if (route.screen === 'add') loadForm(route.paperId);
    else if (route.screen === 'export') scheduleExport();
    render();
  }

  function buildFacetPayload() {
    const facets = {};
    FACET_KEYS.forEach(key => { if (state.facetFilters[key] && state.facetFilters[key].length) facets[key] = state.facetFilters[key]; });
    Object.entries(state.floors).forEach(([key, value]) => { if (value != null && value > 0) facets[key] = value; });
    return facets;
  }

  async function loadPapers() {
    if (!state.session && state.config.auth_required) return;
    const requestId = ++papersRequest;
    const params = new URLSearchParams({ q: state.q, facets: JSON.stringify(buildFacetPayload()), limit: String(state.limit), offset: String(state.offset) });
    state.loadingPapers = true;
    render();
    try {
      const data = await api(`/api/papers?${params}`);
      if (requestId !== papersRequest) return;
      state.papers = Array.isArray(data.papers) ? data.papers : [];
      state.total = Number(data.total || 0);
      state.libraryTotal = Number(data.library_total == null ? state.libraryTotal : data.library_total);
      state.elapsed = data.elapsed_ms == null ? null : data.elapsed_ms;
      state.sort = data.sort || state.sort;
      state.resultFacets = data.facets || {};
      state.queryError = '';
      if (state.selectedId && !state.papers.some(p => String(p.id) === String(state.selectedId)) && state.papers.length) {
        state.selectedId = state.papers[0].id;
      } else if (!state.selectedId && state.papers.length) state.selectedId = state.papers[0].id;
      if (state.selectedId && state.screen === 'library') loadPaper(state.selectedId);
    } catch (error) {
      if (requestId !== papersRequest) return;
      state.queryError = getError(error);
    } finally {
      if (requestId === papersRequest) { state.loadingPapers = false; render(); }
    }
  }

  async function loadPaper(id) {
    if (!id || !state.session && state.config.auth_required) return;
    const requestId = ++paperRequest;
    state.selectedLoading = true;
    render();
    try {
      const params = new URLSearchParams({ id: String(id) });
      if (state.q) params.set('q', state.q);
      const detail = await api(`/api/paper?${params}`);
      if (requestId !== paperRequest || String(state.selectedId) !== String(id)) return;
      state.selectedDetail = detail;
      state.selectedId = state.selectedDetail.id || id;
    } catch (error) {
      if (requestId !== paperRequest || String(state.selectedId) !== String(id)) return;
      state.selectedDetail = null;
      toast(getError(error), true);
    } finally {
      if (requestId === paperRequest) { state.selectedLoading = false; render(); }
    }
  }

  async function loadSmartLists() {
    if (state.smartListsLoaded || (state.config.auth_required && !state.session)) return;
    try {
      const data = await api('/api/smart-lists');
      state.smartLists = Array.isArray(data.lists) ? data.lists : [];
    } catch (error) { state.smartLists = []; }
    state.smartListsLoaded = true;
    render();
  }

  function queryForSmartList() {
    const parts = [];
    if (state.q.trim()) parts.push(state.q.trim());
    FACET_KEYS.forEach(key => {
      (state.facetFilters[key] || []).forEach(value => {
        if (key === 'venue_class') return; // The query grammar intentionally has no venue-class field.
        parts.push(`${key === 'has' ? 'has' : key}:${value}`);
      });
    });
    Object.entries(state.floors).forEach(([key, value]) => { if (value != null && value > 0) parts.push(`${key}:>=${value}`); });
    return parts.join(' AND ');
  }
  async function saveQueryList() {
    const name = String(state.paletteListName || '').trim();
    if (!name) { toast('Enter a smart-list name', true); return; }
    try {
      await api('/api/smart-lists', { method: 'POST', body: { name, query: state.q.trim(), facets: buildFacetPayload(), kind: 'query' } });
      state.paletteListName = '';
      state.smartListsLoaded = false;
      await loadSmartLists();
      toast('Query saved as smart list');
    } catch (error) { toast(getError(error), true); }
  }
  async function deleteSmartList(list) {
    if (!list || list.id == null) return;
    try {
      await api(`/api/smart-lists/${encodeURIComponent(list.id)}`, { method: 'DELETE' });
      state.smartLists = state.smartLists.filter(item => String(item.id) !== String(list.id));
      render();
      toast('Smart list deleted');
    } catch (error) { toast(getError(error), true); }
  }
  function applySavedFacets(facets) {
    const saved = facets && typeof facets === 'object' ? facets : {};
    FACET_KEYS.forEach(key => { state.facetFilters[key] = Array.isArray(saved[key]) ? saved[key].map(String) : []; });
    Object.keys(state.floors).forEach(key => { state.floors[key] = saved[key] == null ? null : Number(saved[key]) || null; });
  }

  async function loadStats() {
    if (statsPromise) return statsPromise;
    state.statsLoading = true;
    render();
    statsPromise = api('/api/stats').then(data => {
      state.stats = data || {};
      state.statsError = '';
    }).catch(error => {
      state.statsError = getError(error);
      state.stats = null;
    }).finally(() => {
      state.statsLoading = false;
      statsPromise = null;
      render();
    });
    return statsPromise;
  }

  function blankForm() {
    const firstField = state.config.fields && state.config.fields[0];
    return {
      id: null,
      identifier: '',
      title: '',
      authors: '',
      venue: '',
      year: '',
      field: firstField ? firstField.id : '',
      state: 'unreviewed',
      starred: false,
      abstract: '',
      canonical_url: '',
      arxiv_id: '',
      doi: '',
      notes: '',
      tags: '',
      scores: { rel: null, cred: null, qual: null, reimpl: null },
      proposed: { rel: null, cred: null, qual: null, reimpl: null },
      proposed_reason: '',
      artifacts: [],
      activeScore: 'rel'
    };
  }

  function authorsToText(authors) {
    if (Array.isArray(authors)) return authors.join('; ');
    return text(authors);
  }
  function tagsToText(tags) {
    if (Array.isArray(tags)) return tags.join(', ');
    return text(tags);
  }
  function formFromPaper(paper, identifier = '') {
    const f = blankForm();
    if (!paper) { f.identifier = identifier; return f; }
    f.id = paper.id || null;
    f.identifier = identifier || paper.doi || paper.arxiv_id || paper.canonical_url || paper.id || '';
    f.title = paper.title || '';
    f.authors = authorsToText(paper.authors);
    f.venue = paper.venue || '';
    f.year = paper.year == null ? '' : paper.year;
    f.field = paper.field || f.field;
    f.state = paper.state || 'unreviewed';
    f.starred = Boolean(paper.starred);
    f.abstract = paper.abstract || '';
    f.canonical_url = paper.canonical_url || '';
    f.arxiv_id = paper.arxiv_id || '';
    f.doi = paper.doi || '';
    f.notes = paper.notes || '';
    f.tags = tagsToText(paper.tags);
    SCORE_KEYS.forEach(key => { f.scores[key] = score(paper[key]); f.proposed[key] = score(paper[`proposed_${key}`]); });
    f.proposed_reason = paper.proposed_reason || '';
    f.artifacts = Array.isArray(paper.artifacts) ? paper.artifacts.map(a => ({ kind: a.kind || 'other', url: a.url || '', local_path: a.local_path || '', fetch_status: a.fetch_status || '' })) : [];
    return f;
  }

  async function loadForm(id) {
    if (state.form && ((id && String(state.form.id) === String(id)) || (!id && !state.form.id))) return;
    if (state.formPromise) return state.formPromise;
    state.formLoading = true;
    state.form = id ? null : blankForm();
    render();
    state.formPromise = (id ? api(`/api/paper?id=${encodeURIComponent(id)}`) : Promise.resolve(null)).then(paper => {
      state.form = formFromPaper(paper);
      state.formError = '';
      if (paper) {
        state.selectedDetail = paper;
        state.selectedId = paper.id;
      }
    }).catch(error => {
      state.formError = getError(error);
      state.form = blankForm();
    }).finally(() => {
      state.formLoading = false;
      state.formPromise = null;
      render();
      if (state.form && state.form.title) checkDuplicates();
    });
    return state.formPromise;
  }

  function updateForm(key, value) {
    if (!state.form) state.form = blankForm();
    state.form[key] = value;
    if (key === 'title' || key === 'authors') {
      window.clearTimeout(duplicateTimer);
      duplicateTimer = window.setTimeout(checkDuplicates, 350);
    }
  }

  async function fetchMetadata() {
    if (!state.form) return;
    const identifier = String(state.form.identifier || '').trim();
    if (!identifier) { toast('Enter a DOI, arXiv id or URL first', true); return; }
    const button = document.querySelector('[data-action="fetch-metadata"]');
    if (button) button.disabled = true;
    try {
      const data = await api('/api/metadata', { method: 'POST', body: { identifier } });
      const previous = state.form;
      const fetched = formFromPaper(data, identifier);
      state.form = {
        ...previous,
        ...fetched,
        id: previous.id || fetched.id || (data && data.id) || null,
        identifier,
        notes: previous.notes,
        tags: previous.tags,
        scores: previous.scores,
        proposed: previous.proposed,
        proposed_reason: previous.proposed_reason,
        artifacts: previous.artifacts,
        state: previous.id ? previous.state : fetched.state,
        starred: previous.id ? previous.starred : fetched.starred
      };
      state.formError = '';
      toast('Metadata fetched');
      render();
      checkDuplicates();
    } catch (error) {
      state.formError = getError(error);
      render();
      toast(state.formError, true);
    } finally { if (button) button.disabled = false; }
  }

  async function checkDuplicates() {
    if (!state.form || !state.form.title.trim()) { state.duplicateCandidates = []; render(); return; }
    state.duplicateLoading = true;
    try {
      const authors = splitAuthors(state.form.authors);
      const data = await api('/api/duplicates', { method: 'POST', body: { title: state.form.title, authors, id: state.form.id || undefined } });
      state.duplicateCandidates = Array.isArray(data.candidates) ? data.candidates : [];
    } catch (_) { state.duplicateCandidates = []; }
    state.duplicateLoading = false;
    render();
  }

  function splitAuthors(value) {
    return String(value || '').split(/\s*;\s*|\s+and\s+/).map(s => s.trim()).filter(Boolean);
  }
  function splitTags(value) { return String(value || '').split(',').map(s => s.trim()).filter(Boolean); }

  function formPayload() {
    const f = state.form || blankForm();
    const payload = {
      title: f.title.trim(), authors: splitAuthors(f.authors), venue: f.venue.trim(), year: number(f.year), field: f.field,
      state: f.state || 'unreviewed', starred: Boolean(f.starred), abstract: f.abstract, canonical_url: f.canonical_url,
      arxiv_id: f.arxiv_id, doi: f.doi, notes: f.notes, tags: splitTags(f.tags),
      artifacts: (f.artifacts || []).filter(a => a.url || a.local_path).map(a => ({ kind: a.kind, url: a.url, local_path: a.local_path })),
      rel: score(f.scores.rel), cred: score(f.scores.cred), qual: score(f.scores.qual), reimpl: score(f.scores.reimpl),
      proposed_rel: score(f.proposed.rel), proposed_cred: score(f.proposed.cred), proposed_qual: score(f.proposed.qual), proposed_reimpl: score(f.proposed.reimpl),
      proposed_reason: f.proposed_reason
    };
    if (f.id) payload.id = f.id;
    Object.keys(payload).forEach(k => { if (payload[k] === undefined) delete payload[k]; });
    return payload;
  }

  async function savePaper(goNext = false) {
    if (!state.form || !state.form.title.trim()) { toast('Title is required', true); return null; }
    const buttons = [...document.querySelectorAll('[data-action="save-paper"], [data-action="save-next"]')];
    buttons.forEach(b => { b.disabled = true; });
    try {
      const payload = formPayload();
      if (goNext && payload.state === 'unreviewed') payload.state = 'reviewed';
      const data = await api('/api/papers', { method: 'POST', body: payload });
      const paper = data && data.paper ? data.paper : data;
      toast(data && data.duplicate ? `Saved; duplicate candidate ${data.duplicate.id || ''}` : 'Paper saved');
      if (goNext) {
        const next = await api('/api/papers?q=state%3Aunreviewed&facets=%7B%7D&limit=100&offset=0');
        const rows = Array.isArray(next.papers) ? next.papers : [];
        const nextPaper = rows.find(row => !paper || String(row.id) !== String(paper.id));
        if (nextPaper) { state.form = null; navigate('add', nextPaper.id); }
        else { state.form = null; state.papers = []; navigate('library'); loadPapers(); }
      } else {
        state.form = null;
        state.papers = [];
        state.selectedDetail = null;
        navigate('library', paper && paper.id ? paper.id : null);
        loadPapers();
      }
      return paper;
    } catch (error) {
      state.formError = getError(error);
      render();
      toast(state.formError, true);
      return null;
    } finally { buttons.forEach(b => { b.disabled = false; }); }
  }

  async function acceptDraft() {
    if (!state.form) return;
    SCORE_KEYS.forEach(key => { if (state.form.proposed[key] != null) state.form.scores[key] = state.form.proposed[key]; });
    render();
    toast('Draft scores accepted locally; save to persist');
  }

  function selectPaper(id, additive = false) {
    state.selectedId = id;
    if (additive) {
      if (state.selection.has(id)) state.selection.delete(id); else state.selection.add(id);
    }
    loadPaper(id);
    render();
  }

  function setView(view) {
    state.view = (view + VIEW_NAMES.length) % VIEW_NAMES.length;
    try { localStorage.setItem('corpus.view', String(state.view)); } catch (_) {}
    render();
  }

  function toggleFacet(key, value, checked) {
    const values = state.facetFilters[key] || [];
    state.facetFilters[key] = checked ? [...new Set([...values, String(value)])] : values.filter(v => String(v) !== String(value));
    state.offset = 0;
    loadPapers();
  }

  function clearFacets() {
    FACET_KEYS.forEach(key => { state.facetFilters[key] = []; });
    Object.keys(state.floors).forEach(key => { state.floors[key] = null; });
    state.offset = 0;
    loadPapers();
  }

  function onQuery(value) {
    state.q = value;
    state.offset = 0;
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(loadPapers, 170);
  }

  async function runSql(save = false) {
    const sql = state.sql.text.trim();
    if (!sql) { toast('Enter a SELECT statement', true); return; }
    state.sql.loading = true;
    state.sql.error = '';
    render();
    try {
      const data = await api('/api/sql', { method: 'POST', body: { sql } });
      state.sql.result = data || { columns: [], rows: [] };
      if (save) {
        const name = state.sql.smartName.trim();
        if (!name) { toast('Add a smart-list name before saving', true); }
        else {
          await api('/api/smart-lists', { method: 'POST', body: { name, query: sql, kind: 'sql' } });
          state.smartListsLoaded = false;
          await loadSmartLists();
          toast('SQL saved as smart list');
        }
      }
    } catch (error) { state.sql.error = getError(error); state.sql.result = null; }
    finally { state.sql.loading = false; render(); }
  }

  function scheduleExport() {
    window.clearTimeout(exportTimer);
    exportTimer = window.setTimeout(runExport, 100);
  }
  async function runExport() {
    if (state.screen !== 'export' || (state.config.auth_required && !state.session)) return;
    state.export.loading = true;
    state.export.error = '';
    render();
    try {
      const e = state.export;
      const body = { scope: e.scope, q: state.q, facets: buildFacetPayload(), ids: [...state.selection], format: e.format, include: e.include };
      const data = await api('/api/export', { method: 'POST', body });
      state.export.preview = text(data.text);
      state.export.filename = data.filename || `corpus.${e.format === 'bibtex' ? 'bib' : e.format}`;
      state.export.mime = data.mime || 'text/plain;charset=utf-8';
      state.export.count = Number(data.count || 0);
    } catch (error) { state.export.error = getError(error); state.export.preview = ''; }
    finally { state.export.loading = false; render(); }
  }
  async function copyExport() {
    try { await copyText(state.export.preview || ''); toast('Export copied'); }
    catch (error) { toast(getError(error), true); }
  }
  async function copyText(value) {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value);
    else {
      const input = h('textarea', { value });
      input.style.position = 'fixed'; input.style.opacity = '0'; document.body.appendChild(input); input.select(); document.execCommand('copy'); input.remove();
    }
  }
  function bibtexForPaper(paper) {
    const key = paper.bibtex_key || paper.id || 'paper';
    const author = Array.isArray(paper.authors) ? paper.authors.join(' and ') : text(paper.authors);
    const lines = [
      `@article{${key},`,
      `  title = {${text(paper.title)}},`,
      `  author = {${author}},`,
      paper.venue ? `  journal = {${paper.venue}},` : '',
      paper.year == null ? '' : `  year = {${paper.year}},`,
      paper.canonical_url ? `  url = {${paper.canonical_url}}` : paper.doi ? `  doi = {${paper.doi}}` : ''
    ].filter(Boolean);
    return `${lines.join('\n')}\n}`;
  }
  async function copyPaperBibtex(paper) {
    try { await copyText(bibtexForPaper(paper)); toast('BibTeX copied'); }
    catch (error) { toast(getError(error), true); }
  }
  function downloadExport() {
    const blob = new Blob([state.export.preview || ''], { type: state.export.mime || 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = h('a', { href: url, download: state.export.filename || 'corpus-export.txt' });
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function openPdf(paper) {
    const detail = paper || state.selectedDetail;
    const artifacts = detail && Array.isArray(detail.artifacts) ? detail.artifacts : [];
    const pdf = artifacts.find(a => String(a.kind).toLowerCase() === 'pdf');
    if (!pdf) { toast('No PDF artifact is available', true); return; }
    const target = pdf.local_path ? `/api/artifacts/${encodeURIComponent(pdf.id || detail.id)}/file` : pdf.url;
    if (!target) { toast('PDF artifact has no URL', true); return; }
    window.open(target, '_blank', 'noopener');
  }

  function openCanonical(paper) {
    const target = paper && (paper.canonical_url || paper.url);
    if (!target) { toast('This paper has no canonical URL', true); return; }
    window.open(target, '_blank', 'noopener');
  }

  async function queueArtifact(artifactId, retry = false) {
    if (!artifactId) return;
    try {
      const endpoint = retry ? 'retry' : 'cache';
      const result = await api(`/api/artifacts/${encodeURIComponent(artifactId)}/${endpoint}`, { method: 'POST' });
      toast(result && result.queued === false ? 'PDF was not queued' : (retry ? 'PDF retry queued' : 'PDF cache queued'));
      if (state.selectedId) loadPaper(state.selectedId);
      if (state.screen === 'stats') { state.stats = null; loadStats(); }
    } catch (error) { toast(getError(error), true); }
  }

  function updateStarred(paper) {
    if (!paper || !paper.id) return;
    api('/api/papers', { method: 'POST', body: { id: paper.id, starred: !paper.starred } }).then(data => {
      const updated = data && data.paper ? data.paper : data;
      state.selectedDetail = updated || { ...paper, starred: !paper.starred };
      const row = state.papers.find(p => String(p.id) === String(paper.id));
      if (row) row.starred = !paper.starred;
      render();
    }).catch(error => toast(getError(error), true));
  }

  function formatDate(value) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value).slice(0, 10);
    const days = Math.max(0, Math.floor((Date.now() - date.getTime()) / 86400000));
    if (days < 1) return 'today';
    if (days < 30) return `${days}d ago`;
    return date.toISOString().slice(0, 10);
  }
  function formatBytes(value) {
    const n = number(value);
    if (n == null) return '—';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
    return `${(n / 1024 / 1024 / 1024).toFixed(1)} GB`;
  }
  function artifactKinds(paper) { return Array.isArray(paper && paper.artifacts) ? paper.artifacts : []; }

  function renderHeader() {
    const stats = state.stats || {};
    const states = stats.states || {};
    const refs = state.libraryTotal || state.total || 0;
    const unreviewed = states.unreviewed == null ? null : states.unreviewed;
    const summary = unreviewed == null ? `${refs.toLocaleString()} REFS` : `${refs.toLocaleString()} REFS · ${unreviewed} unreviewed`;
    const nav = h('nav', { class: 'screen-nav', ariaLabel: 'Primary navigation' }, ...['library', 'stats', 'add', 'export'].map(screen => h('a', {
      href: routeUrl(screen), ariaCurrent: state.screen === screen ? 'page' : null,
      onclick: event => { event.preventDefault(); navigate(screen); }
    }, screen[0].toUpperCase() + screen.slice(1))));
    const actions = state.config.auth_required ? h('button', { class: 'header-action', type: 'button', onclick: logout }, 'LOG OUT') : null;
    return h('header', { class: 'topbar' },
      h('div', { class: 'brand-block' }, h('span', { class: 'brand' }, 'Corpus'), h('span', { class: 'brand-count' }, summary)),
      nav,
      h('div', { class: 'ingest-summary' }, latestRunSummary(stats, unreviewed)),
      actions
    );
  }
  function latestRunSummary(stats, unreviewed) {
    const runs = Array.isArray(stats.runs) ? stats.runs : [];
    const latest = runs[0];
    if (latest) return `ingest ${formatDate(latest.timestamp)} · +${Number(latest.proposed || 0)} proposed`;
    if (unreviewed != null) return `${unreviewed} unreviewed`;
    return 'ingest —';
  }

  function renderQueryBar() {
    const query = h('div', { class: 'query-wrap' },
      h('span', { class: 'query-prompt', ariaHidden: 'true' }, '>'),
      h('input', { id: 'query-input', 'data-focus-key': 'query-input', class: 'query-input', type: 'search', value: state.q, placeholder: 'field:sbi-pretrain AND rel:>7 AND pdf:"posterior collapse"', ariaLabel: 'Search query', oninput: event => onQuery(event.target.value), onkeydown: event => { if (event.key === 'Escape') { event.target.value = ''; onQuery(''); } } }),
      h('span', { class: 'query-result' }, state.loadingPapers ? 'loading…' : `${state.total} / ${state.libraryTotal || state.total} rows${state.elapsed == null ? '' : ` · ${state.elapsed} ms`}`)
    );
    return h('div', { class: 'query-toolbar' }, query,
      h('button', { class: 'toolbar-button', type: 'button', onclick: () => { state.showConsole = !state.showConsole; render(); } }, 'SQL'),
      h('button', { class: 'toolbar-button', type: 'button', onclick: () => setView(state.view + 1) }, `VIEW: ${VIEW_NAMES[state.view]}`),
      h('button', { class: 'toolbar-button', type: 'button', onclick: () => { state.showFacets = !state.showFacets; render(); }, ariaLabel: 'Toggle facet rail' }, state.showFacets ? 'FACETS' : 'NO FACETS')
    );
  }

  function renderSql() {
    const defaultSql = 'select id, title, rel, reimpl from papers\nwhere field = \'sbi-pretrain\' and rel > 7\norder by reimpl desc;';
    if (!state.showConsole) return null;
    const sqlEditor = h('textarea', { id: 'sql-editor', 'data-focus-key': 'sql-editor', class: 'sql-editor', spellcheck: 'false', placeholder: defaultSql, value: state.sql.text, ariaLabel: 'SQL console', oninput: event => { state.sql.text = event.target.value; }, onkeydown: event => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') { event.preventDefault(); runSql(Boolean(event.shiftKey)); } } });
    const controls = h('div', { class: 'sql-actions' },
      h('button', { class: 'btn', type: 'button', onclick: () => runSql(false), disabled: state.sql.loading }, state.sql.loading ? 'RUNNING…' : 'RUN ⏎'),
      h('input', { id: 'sql-smart-name', 'data-focus-key': 'sql-smart-name', class: 'input', value: state.sql.smartName, placeholder: 'smart list name', ariaLabel: 'Smart list name', oninput: event => { state.sql.smartName = event.target.value; } }),
      h('button', { class: 'btn', type: 'button', onclick: () => runSql(true), disabled: state.sql.loading }, 'RUN + SAVE')
    );
    const wrap = h('div', { class: 'sql-console' }, sqlEditor, controls);
    const result = state.sql.result ? renderSqlResult(state.sql.result) : null;
    if (state.sql.error) wrap.appendChild(h('div', { class: 'status-line error-text' }, state.sql.error));
    return h('div', null, wrap, result);
  }
  function renderSqlResult(result) {
    const columns = Array.isArray(result.columns) ? result.columns : [];
    const rows = Array.isArray(result.rows) ? result.rows : [];
    const table = h('table', { class: 'data-table' },
      h('thead', null, h('tr', null, ...columns.map(column => h('th', null, column)))),
      h('tbody', null, ...rows.map(row => h('tr', null, ...columns.map(column => h('td', null, fmt(row && row[column], ''))))))
    );
    return h('div', { class: 'sql-result', 'data-scroll-key': 'sql-result' }, h('div', { class: 'sql-meta' }, `${rows.length} rows${result.elapsed_ms == null ? '' : ` · ${result.elapsed_ms} ms`}${result.truncated ? ' · truncated' : ''}`), table);
  }

  function facetRows(key) {
    const raw = state.resultFacets && state.resultFacets[key];
    if (!Array.isArray(raw)) return [];
    return raw.map(item => typeof item === 'object' && item !== null ? { value: item.value == null ? item.label : item.value, count: item.count } : { value: item, count: null }).filter(item => item.value != null);
  }
  function renderFacets() {
    const sections = [
      ['field', 'FIELD'], ['state', 'STATUS'], ['has', 'ARTIFACTS'], ['venue_class', 'VENUE'], ['year', 'YEAR']
    ].map(([key, title]) => {
      const rows = facetRows(key);
      if (!rows.length) return null;
      return h('section', { class: 'facet-section' }, h('div', { class: 'facet-title' }, title), ...rows.map(row => {
        const checked = (state.facetFilters[key] || []).some(value => String(value) === String(row.value));
        return h('label', { class: 'facet-option' },
          h('input', { type: 'checkbox', checked, onchange: event => toggleFacet(key, row.value, event.target.checked) }),
          h('span', { class: 'facet-label' }, key === 'field' ? fieldLabel(row.value) : row.value),
          row.count == null ? null : h('span', { class: 'facet-count' }, row.count)
        );
      }));
    });
    const floorSection = h('section', { class: 'facet-section' }, h('div', { class: 'facet-title' }, 'SCORE FLOORS'), ...SCORE_KEYS.map(key => {
      const value = state.floors[key] == null ? 0 : state.floors[key];
      return h('div', { class: 'slider-row' }, h('label', { for: `floor-${key}` }, key), h('input', { id: `floor-${key}`, type: 'range', min: 0, max: 10, step: 1, value, style: { background: `linear-gradient(to right, var(--accent) 0%, var(--accent) ${value * 10}%, var(--neutral-300) ${value * 10}%, var(--neutral-300) 100%)` }, ariaLabel: `${key} score floor`, oninput: event => { state.floors[key] = Number(event.target.value) || null; event.target.style.background = `linear-gradient(to right, var(--accent) 0%, var(--accent) ${Number(event.target.value) * 10}%, var(--neutral-300) ${Number(event.target.value) * 10}%, var(--neutral-300) 100%)`; }, onchange: () => { state.offset = 0; loadPapers(); } }), h('span', { class: 'slider-value' }, value || '—'));
    }));
    return h('aside', { class: 'facet-rail' }, ...sections, floorSection, h('button', { class: 'btn facet-clear', type: 'button', onclick: clearFacets }, 'CLEAR FACETS'));
  }

  function scoreCell(value, withBars) {
    const n = score(value);
    if (!withBars) return h('span', { class: `score-value ${scoreClass(n)}` }, scoreLabel(n));
    return h('span', { class: `score-value ${scoreClass(n)}` }, h('span', { class: 'score-bar', ariaHidden: 'true' }, h('i', { class: n != null && n <= 5 ? 'low' : '', style: { width: n == null ? '0%' : `${n * 10}%` } })), scoreLabel(n));
  }
  function renderTable() {
    const withBars = state.view === 1;
    const ledger = state.view === 2;
    const headCell = (label, className = '') => h('div', { class: `table-cell ${className}` }, label);
    const head = h('div', { class: 'table-head' }, headCell('', 'gutter'), headCell('TITLE / AUTHORS', 'title'), headCell('VENUE', 'venue'), headCell('YR', 'year'), ...SCORE_KEYS.map(key => headCell(key.toUpperCase(), 'score')), headCell('ART', 'artifacts'), headCell('ADDED', 'added'));
    const body = state.papers.length ? state.papers.map((paper, index) => {
      const artifacts = artifactKinds(paper);
      const hasCode = artifacts.some(a => String(a.kind).toLowerCase() === 'code');
      const flag = state.selection.has(paper.id) ? '▪' : paper.state === 'unreviewed' ? '○' : paper.starred ? '★' : '';
      const rowClass = `paper-row${state.selectedId != null && String(state.selectedId) === String(paper.id) ? ' selected' : ''}${ledger ? ' zebra' : ''}`;
      return h('div', { class: rowClass, role: 'row', tabIndex: 0, ariaSelected: state.selectedId != null && String(state.selectedId) === String(paper.id), onclick: () => selectPaper(paper.id), onkeydown: event => { if (event.key === 'Enter') { event.preventDefault(); if (event.shiftKey) openPdf(state.selectedDetail || paper); else openCanonical(state.selectedDetail || paper); } } },
        h('div', { class: 'table-cell gutter' }, flag),
        h('div', { class: 'table-cell title' }, h('span', { class: 'title-text', title: paper.title }, fmt(paper.title)), h('span', { class: 'authors' }, authorsToText(paper.authors))),
        h('div', { class: 'table-cell venue', title: paper.venue || '' }, fmt(paper.venue)),
        h('div', { class: 'table-cell year' }, fmt(paper.year)),
        ...SCORE_KEYS.map(key => h('div', { class: 'table-cell score' }, scoreCell(paper[key], withBars))),
        h('div', { class: 'table-cell artifacts' }, artifacts.some(a => String(a.kind).toLowerCase() === 'pdf') ? h('span', { class: 'artifact-badge' }, 'PDF') : null, hasCode ? h('span', { class: 'artifact-badge code' }, 'SRC') : null),
        h('div', { class: 'table-cell added' }, formatDate(paper.added || paper.date_added))
      );
    }) : [h('div', { class: 'empty' }, state.loadingPapers ? 'Loading papers…' : state.queryError ? 'Previous results retained.' : 'No papers match this query.')];
    const page = h('div', { class: 'pagination' },
      h('button', { class: 'icon-button', type: 'button', ariaLabel: 'Previous page', disabled: state.offset <= 0, onclick: () => { state.offset = Math.max(0, state.offset - state.limit); loadPapers(); } }, '‹'),
      h('span', null, `${state.total ? state.offset + 1 : 0}–${Math.min(state.offset + state.limit, state.total)} / ${state.total}`),
      h('button', { class: 'icon-button', type: 'button', ariaLabel: 'Next page', disabled: state.offset + state.limit >= state.total, onclick: () => { state.offset += state.limit; loadPapers(); } }, '›')
    );
    return h('main', { class: `table-region ${withBars ? 'bars-view' : ''} ${ledger ? 'ledger-view' : ''}` }, h('div', { class: 'table-scroll', 'data-scroll-key': 'table-scroll' }, h('div', { class: 'paper-table', role: 'table', ariaLabel: 'Paper library' }, head, ...body)), h('div', { class: 'table-footer' }, h('span', null, `sort: ${state.sort || 'rel'} ↓`), h('span', null, 'j/k move · ⏎ open · x select · ⇧⏎ PDF · ⌘k palette'), page, h('span', { class: 'push' }, VIEW_NOTES[state.view])));
  }

  function sanitizeHighlightedHTML(value) {
    const source = String(value || '');
    const wrapper = document.createElement('div');
    const parser = new DOMParser();
    const parsed = parser.parseFromString(`<div>${source}</div>`, 'text/html').body.firstElementChild;
    if (!parsed) return wrapper;
    (function walk(from, to) {
      [...from.childNodes].forEach(child => {
        if (child.nodeType === Node.TEXT_NODE) to.appendChild(document.createTextNode(child.nodeValue));
        else if (child.nodeType === Node.ELEMENT_NODE) {
          const mark = child.tagName.toLowerCase() === 'mark' ? document.createElement('mark') : null;
          if (mark) { mark.textContent = child.textContent || ''; to.appendChild(mark); }
          else walk(child, to);
        }
      });
    })(parsed, wrapper);
    return wrapper;
  }
  function sanitizeNotesHTML(value) {
    const wrapper = document.createElement('div');
    const parsed = new DOMParser().parseFromString(`<div>${String(value || '')}</div>`, 'text/html').body.firstElementChild;
    const allowed = new Set(['p', 'em', 'strong', 'code', 'pre', 'ul', 'ol', 'li', 'a', 'br']);
    if (!parsed) return wrapper;
    (function walk(from, to) {
      [...from.childNodes].forEach(child => {
        if (child.nodeType === Node.TEXT_NODE) to.appendChild(document.createTextNode(child.nodeValue));
        else if (child.nodeType === Node.ELEMENT_NODE) {
          const name = child.tagName.toLowerCase();
          if (!allowed.has(name)) { walk(child, to); return; }
          const out = document.createElement(name);
          if (name === 'a') {
            const href = child.getAttribute('href') || '';
            if (/^https?:\/\//i.test(href)) { out.setAttribute('href', href); out.setAttribute('target', '_blank'); out.setAttribute('rel', 'noopener'); }
          }
          to.appendChild(out); walk(child, out);
        }
      });
    })(parsed, wrapper);
    return wrapper;
  }

  function renderDetail() {
    const paper = state.selectedDetail;
    if (!state.showDetail) return null;
    if (!paper) return h('aside', { class: 'detail-pane', 'data-scroll-key': 'detail-pane' }, h('div', { class: 'empty' }, state.selectedLoading ? 'Loading paper…' : 'Select a paper to inspect it.'));
    const artifacts = artifactKinds(paper);
    const proposed = SCORE_KEYS.map(key => score(paper[`proposed_${key}`]));
    const proposedText = proposed.some(v => v != null) ? proposed.map(v => scoreLabel(v)).join('/') : '—';
    const scoreRows = SCORE_KEYS.map(key => {
      const n = score(paper[key]);
      return h('div', { class: 'detail-score-row' }, h('span', { class: 'detail-score-label' }, SCORE_LABELS[key]), h('span', { class: 'detail-score-track' }, h('i', { style: { width: n == null ? '0%' : `${n * 10}%` } })), h('span', { class: 'detail-score-value' }, scoreLabel(n), n == null ? '' : '/10'));
    });
    const hitNodes = Array.isArray(paper.pdf_hits) && paper.pdf_hits.length ? paper.pdf_hits.map(hit => h('div', { class: 'pdf-hit' }, `p.${fmt(hit.page, '?')} — `, sanitizeHighlightedHTML(hit.snippet))) : [h('div', { class: 'soft' }, 'No PDF full-text hits for this query.')];
    const prov = Array.isArray(paper.provenance) && paper.provenance.length ? paper.provenance.map(item => h('div', null, `${fmt(item.agent, 'agent')} · run ${fmt(item.run_id, '—')}${item.status ? ` · ${item.status}` : ''}${item.query ? ` · ${item.query}` : ''}${item.similarity == null ? '' : ` · dedupe similarity ${item.similarity}`}${item.reason ? ` · ${item.reason}` : ''}`)) : [h('div', null, 'No ingest provenance recorded.')];
    const artifactLinks = artifacts.length ? artifacts.map((artifact, index) => {
      const isCached = Boolean(artifact.local_path);
      const target = isCached ? `/api/artifacts/${encodeURIComponent(artifact.id || paper.id)}/file` : artifact.url;
      const link = target ? h('a', { class: 'artifact-link', href: target, target: '_blank', rel: 'noopener', download: isCached ? '' : null }, `${artifact.kind || 'artifact'}${artifact.fetch_status === 'failed' ? ' !' : ''} ↗`) : h('span', { class: 'artifact-link' }, artifact.kind || `artifact ${index + 1}`);
      const canQueue = String(artifact.kind).toLowerCase() === 'pdf' && !isCached && artifact.id && ['linked', 'failed'].includes(String(artifact.fetch_status || 'linked'));
      const status = artifact.fetch_status === 'pending' ? ' · queued' : artifact.fetch_status === 'fetching' ? ' · fetching' : artifact.fetch_status === 'failed' ? ' !' : '';
      const statusLink = target && status ? h('span', { class: 'soft', style: { font: '9px var(--mono)' } }, status) : null;
      return h('span', { class: 'artifact-entry' }, link, statusLink, canQueue ? h('button', { class: 'btn', type: 'button', onclick: () => queueArtifact(artifact.id, artifact.fetch_status === 'failed') }, artifact.fetch_status === 'failed' ? 'RETRY' : 'CACHE') : null);
    }) : [h('span', { class: 'soft' }, 'No artifacts')];
    const paperLinks = [];
    if (paper.canonical_url) paperLinks.push(h('a', { class: 'artifact-link', href: paper.canonical_url, target: '_blank', rel: 'noopener' }, paper.arxiv_id ? 'abs page ↗' : 'paper ↗'));
    paperLinks.push(h('button', { class: 'artifact-link', type: 'button', onclick: () => copyPaperBibtex(paper) }, 'bibtex'));
    const noteNode = paper.notes_html ? sanitizeNotesHTML(paper.notes_html) : paper.notes ? document.createTextNode(paper.notes) : h('span', { class: 'soft' }, 'No note');
    return h('aside', { class: 'detail-pane', 'data-scroll-key': 'detail-pane' },
      h('div', { class: 'detail-header' }, h('div', { class: 'detail-id' }, h('span', null, fmt(paper.arxiv_id || paper.doi || paper.id)), h('span', null, fieldLabel(paper.field))), h('div', { class: 'detail-title' }, fmt(paper.title)), h('div', { class: 'detail-meta' }, `${authorsToText(paper.authors)} · ${fmt(paper.venue)} ${fmt(paper.year, '')}`), h('div', { class: 'detail-actions' }, h('button', { class: 'btn btn-primary', type: 'button', onclick: () => navigate('add', paper.id) }, 'EDIT'), h('button', { class: 'btn', type: 'button', onclick: () => updateStarred(paper) }, paper.starred ? 'UNSTAR' : 'STAR'), h('button', { class: 'btn', type: 'button', onclick: () => openPdf(paper) }, 'PDF'))),
      h('section', { class: 'detail-section' }, h('div', { class: 'panel-title' }, 'SCORES'), ...scoreRows, h('div', { class: 'soft', style: { marginTop: '3px', font: '9.5px var(--mono)' } }, `${SCORE_KEYS.some(key => score(paper[key]) != null) ? 'scored by me' : 'not yet scored'} · draft ${proposedText}${paper.proposed_reason ? ` · ${paper.proposed_reason}` : ''}`)),
      h('section', { class: 'detail-section' }, h('div', { class: 'panel-title' }, 'ARTIFACTS'), h('div', { class: 'artifact-list' }, paperLinks, artifactLinks)),
      h('section', { class: 'detail-section' }, h('div', { class: 'panel-title' }, 'MY NOTE'), h('div', { class: 'detail-note' }, noteNode)),
      h('section', { class: 'detail-section' }, h('div', { class: 'panel-title' }, 'PDF FULL-TEXT HITS'), ...hitNodes),
      h('section', { class: 'detail-section' }, h('div', { class: 'panel-title' }, 'TAGS'), h('div', { class: 'tag-list' }, Array.isArray(paper.tags) && paper.tags.length ? paper.tags.map(tag => h('span', { class: 'tag' }, tag)) : h('span', { class: 'soft' }, 'No tags'))),
      h('section', { class: 'detail-section' }, h('div', { class: 'panel-title' }, 'PROVENANCE'), h('div', { class: 'provenance' }, ...prov))
    );
  }

  function renderLibrary() {
    const classes = ['library-layout'];
    if (!state.showFacets) classes.push('no-facets');
    if (!state.showDetail) classes.push('no-detail');
    return h('div', { class: 'screen' }, renderQueryBar(), renderSql(), state.queryError ? h('div', { class: 'notice' }, state.queryError) : null, h('div', { class: classes.join(' ') }, state.showFacets ? renderFacets() : null, renderTable(), renderDetail()));
  }

  function renderStats() {
    if (state.statsLoading && !state.stats) return h('div', { class: 'empty' }, 'Loading statistics…');
    if (state.statsError) return h('div', { class: 'notice error-text' }, state.statsError);
    const stats = state.stats || {};
    const fields = Array.isArray(stats.fields) ? stats.fields : [];
    const maxField = Math.max(1, ...fields.map(f => Number(f.count || 0)));
    const fieldPanel = h('section', { class: 'stats-panel' }, h('div', { class: 'panel-title' }, 'COUNTS BY FIELD'), fields.length ? fields.map(field => h('div', { class: 'field-stat' }, h('span', { class: 'field-stat-name', title: field.label || field.field }, field.label || field.field || 'unfiled'), h('span', { class: 'field-stat-track' }, h('i', { style: { width: `${Number(field.count || 0) / maxField * 100}%` } })), h('span', { class: 'field-stat-count' }, field.count || 0))) : h('div', { class: 'empty' }, 'No field counts yet.'));
    const distributions = stats.distributions || {};
    const distPanel = h('section', { class: 'stats-panel' }, h('div', { class: 'panel-title' }, 'SCORE DISTRIBUTIONS · 0–10'), ...SCORE_KEYS.map(key => {
      const dist = distributions[key] || {};
      const bins = Array.isArray(dist.bins) ? dist.bins : [];
      const max = Math.max(1, ...bins.map(Number));
      return h('div', { class: 'hist-row' }, h('span', { class: 'hist-label' }, SCORE_LABELS[key]), h('span', { class: 'hist-bars' }, ...bins.map((bin, index) => h('i', { class: index >= 8 ? 'high' : '', style: { height: `${Math.max(2, Number(bin || 0) / max * 34)}px` } }))), h('span', { class: 'hist-mean' }, `μ ${fmt(dist.mean)}`));
    }), h('div', { class: 'legend' }, 'gold bins = ≥8 · the shortlist'));
    const runs = Array.isArray(stats.runs) ? stats.runs : [];
    const runMax = Math.max(1, ...runs.map(run => Number(run.proposed || 0)));
    const ingestRuns = runs.slice(0, 14);
    const ingestPanel = h('section', { class: 'stats-panel' }, h('div', { class: 'panel-title' }, 'INGEST · LAST 14 RUNS'), ingestRuns.length ? h('div', { class: 'ingest-chart' }, ...ingestRuns.map(run => h('i', { title: `${fmt(run.timestamp)} · ${run.proposed || 0} proposed`, style: { height: `${Math.max(2, Number(run.proposed || 0) / runMax * 44)}px` } }))) : h('div', { class: 'empty' }, 'No ingest runs yet.'), ingestRuns.length ? h('div', { class: 'axis' }, h('span', null, ingestRuns.length === 1 ? 'latest' : `${ingestRuns.length} runs ago`), h('span', null, 'latest')) : null, h('div', { class: 'legend' }, `${fmt(stats.accepted, 0)} kept / ${fmt(stats.proposed, 0)} proposed · ${stats.keep_rate == null ? '—' : `${stats.keep_rate}%`} keep rate`));
    const runDetails = runs.length ? runs.slice().reverse().map(run => {
      let extra = {};
      try { extra = typeof run.extra === 'string' ? JSON.parse(run.extra || '{}') : (run.extra || {}); } catch (_) { extra = {}; }
      const results = Array.isArray(extra.results) ? extra.results : [];
      const rejected = results.filter(item => item && (item.status === 'reject' || item.status === 'rejected'));
      return h('div', { class: 'failure-row' }, h('div', { class: 'failure-title' }, `run ${fmt(run.id)} · ${fmt(run.agent)} · ${formatDate(run.timestamp)}`), h('div', { class: 'soft' }, `${fmt(run.query, '')} · ${fmt(run.accepted, 0)} accepted · ${fmt(run.rejected, 0)} rejected · ${fmt(run.duplicate, 0)} duplicate`), rejected.length ? h('div', { class: 'soft' }, ...rejected.slice(0, 4).map(item => h('div', null, `reject: ${fmt(item.reason)}`))) : null);
    }) : [h('div', { class: 'empty' }, 'No run details yet.')];
    const runPanel = h('section', { class: 'stats-panel' }, h('div', { class: 'panel-title' }, 'INGEST RUN DETAILS'), ...runDetails);
    const states = stats.states || {};
    const kpis = [['total entries', stats.total], ['unreviewed', states.unreviewed], ['reviewed', states.reviewed], ['archived', states.archived], ['rel ≥ 8 · shortlist', stats.shortlist], ['reimpl ≥ 8 · queue', stats.reimplementation], ['code available', stats.code], ['pdf full-text indexed', stats.indexed], ['pdf pending', stats.pdf_pending], ['pdf failures', Array.isArray(stats.pdf_failures) ? stats.pdf_failures.length : 0], ['pdf cache', formatBytes(stats.cache_bytes)]];
    const statePanel = h('section', { class: 'stats-panel' }, h('div', { class: 'panel-title' }, 'STATE OF THE LIBRARY'), ...kpis.map(([label, value]) => h('div', { class: 'kpi' }, h('span', { class: 'kpi-label' }, label), h('span', null, fmt(value)))));
    const failures = Array.isArray(stats.pdf_failures) ? stats.pdf_failures : [];
    const failurePanel = h('section', { class: 'stats-panel' }, h('div', { class: 'panel-title' }, 'PDF FAILURES'), failures.length ? failures.map(failure => h('div', { class: 'failure-row' }, h('div', { class: 'failure-title' }, fmt(failure.title, failure.paper_id)), h('div', { class: 'soft' }, `${fmt(failure.url, '')}${failure.error ? ` · ${failure.error}` : ''}`), h('button', { class: 'btn', type: 'button', onclick: () => queueArtifact(failure.artifact_id, true) }, 'RETRY'))) : h('div', { class: 'empty' }, 'No PDF failures recorded.'));
    return h('div', { class: 'stats-grid', 'data-scroll-key': 'stats-grid' }, fieldPanel, distPanel, ingestPanel, statePanel, runPanel, failurePanel);
  }

  function formInput(label, key, options = {}) {
    const controlAttrs = { id: `form-${key}`, 'data-focus-key': `form-${key}` };
    const control = options.multiline ? h('textarea', { ...controlAttrs, class: 'form-control textarea', rows: options.rows || 3, value: state.form[key] || '', placeholder: options.placeholder || '', oninput: event => updateForm(key, event.target.value) }) : options.select ? h('select', { ...controlAttrs, class: 'form-control select', onchange: event => { updateForm(key, event.target.value); } }, ...options.select.map(opt => h('option', { value: opt.id || opt, selected: String(state.form[key] || '') === String(opt.id || opt) }, opt.label || opt))) : h('input', { ...controlAttrs, class: 'form-control', type: options.type || 'text', value: state.form[key] == null ? '' : state.form[key], placeholder: options.placeholder || '', oninput: event => updateForm(key, event.target.value) });
    return h('div', { class: 'form-line' }, h('label', { class: 'form-label', for: `form-${key}` }, label), control);
  }
  function renderScoreEditor() {
    return h('div', { class: 'form-section' }, h('div', { class: 'form-section-label', style: { marginBottom: '4px' } }, 'SCORES · click a cell, or 0–9 on the keyboard'), ...SCORE_KEYS.map(key => h('div', { class: 'score-grid-row' }, h('button', { class: 'score-grid-key', type: 'button', onclick: () => { state.form.activeScore = key; render(); } }, SCORE_LABELS[key]), h('span', { class: 'score-grid' }, ...Array.from({ length: 11 }, (_, value) => h('button', { class: `score-cell${score(state.form.scores[key]) === value ? ' active' : ''}`, type: 'button', onclick: () => { state.form.scores[key] = value; state.form.activeScore = key; render(); } }, value))), h('span', { class: 'score-hint' }, state.form.scores[key] == null ? 'unset' : `${state.form.scores[key]}/10`), h('button', { class: 'btn btn-quiet score-clear', type: 'button', onclick: () => { state.form.scores[key] = null; state.form.activeScore = key; render(); } }, 'clear'))));
  }
  function renderArtifactEditor() {
    const rows = state.form.artifacts || [];
    return h('div', { class: 'form-section' }, h('div', { class: 'form-section-label' }, 'ARTIFACTS'), h('div', { class: 'artifact-editor' }, ...rows.map((artifact, index) => h('div', { class: 'artifact-editor-row' }, h('select', { id: `artifact-${index}-kind`, 'data-focus-key': `artifact-${index}-kind`, class: 'select', value: artifact.kind, ariaLabel: `Artifact ${index + 1} kind`, onchange: event => { artifact.kind = event.target.value; } }, ...ARTIFACT_KINDS.map(kind => h('option', { value: kind, selected: artifact.kind === kind }, kind))), h('input', { id: `artifact-${index}-url`, 'data-focus-key': `artifact-${index}-url`, class: 'input', value: artifact.url || '', placeholder: 'URL', ariaLabel: `Artifact ${index + 1} URL`, oninput: event => { artifact.url = event.target.value; } }), h('button', { class: 'icon-button', type: 'button', ariaLabel: 'Remove artifact', onclick: () => { state.form.artifacts.splice(index, 1); render(); } }, '×'))), h('button', { class: 'btn btn-quiet', type: 'button', onclick: () => { state.form.artifacts.push({ kind: 'pdf', url: '', local_path: '', fetch_status: '' }); render(); } }, '+ ARTIFACT')));
  }
  function renderAdd() {
    if (state.formLoading && !state.form) return h('div', { class: 'empty' }, 'Loading paper…');
    if (!state.form) state.form = blankForm();
    const fields = state.config.fields || [];
    const form = h('form', { class: 'entry-form', onsubmit: event => { event.preventDefault(); savePaper(false); } },
      h('div', { class: 'form-kicker' }, h('strong', null, state.form.id ? 'EDIT ENTRY' : 'ADD ENTRY'), h('span', null, 'paste a DOI, arXiv id or URL and the fields below autofill')),
      h('div', { class: 'form-line identifier-row' }, h('label', { class: 'form-label', for: 'form-identifier' }, 'IDENTIFIER'), h('input', { id: 'form-identifier', 'data-focus-key': 'form-identifier', class: 'form-control', value: state.form.identifier, placeholder: 'doi:… · arXiv:… · https://…', oninput: event => updateForm('identifier', event.target.value) }), h('button', { class: 'btn btn-primary', type: 'button', 'data-action': 'fetch-metadata', onclick: fetchMetadata }, 'FETCH')),
      formInput('TITLE', 'title', { placeholder: 'Paper title' }),
      formInput('AUTHORS', 'authors', { placeholder: 'Author; Author; …' }),
      formInput('VENUE', 'venue', { placeholder: 'Venue' }),
      formInput('YEAR', 'year', { type: 'number', placeholder: 'Year' }),
      formInput('FIELD', 'field', { select: fields.map(field => ({ id: field.id, label: field.label })) }),
      formInput('STATE', 'state', { select: ['unreviewed', 'reviewed', 'archived'] }),
      h('div', { class: 'form-line' }, h('label', { class: 'form-label', for: 'form-starred' }, 'STARRED'), h('input', { id: 'form-starred', 'data-focus-key': 'form-starred', type: 'checkbox', checked: state.form.starred, style: { margin: '8px' }, ariaLabel: 'Star paper', onchange: event => updateForm('starred', event.target.checked) })),
      formInput('CANONICAL URL', 'canonical_url', { placeholder: 'https://…' }),
      formInput('ARXIV ID', 'arxiv_id'),
      formInput('DOI', 'doi'),
      formInput('TAGS', 'tags', { placeholder: 'comma, separated, tags' }),
      formInput('ABSTRACT', 'abstract', { multiline: true, rows: 4 }),
      renderScoreEditor(),
      h('div', { class: 'form-line' }, h('label', { class: 'form-label', for: 'form-notes' }, 'NOTE'), h('textarea', { id: 'form-notes', 'data-focus-key': 'form-notes', class: 'form-control textarea', rows: 3, value: state.form.notes || '', oninput: event => updateForm('notes', event.target.value) })),
      renderArtifactEditor(),
      state.formError ? h('div', { class: 'notice error-text' }, state.formError) : null,
      h('div', { class: 'form-actions' }, h('button', { class: 'btn btn-primary', type: 'submit', 'data-action': 'save-paper' }, 'SAVE'), h('button', { class: 'btn', type: 'button', 'data-action': 'save-next', onclick: () => savePaper(true) }, 'SAVE & NEXT UNREVIEWED'), h('button', { class: 'btn', type: 'button', onclick: () => { state.form = null; navigate('library'); } }, 'DISCARD'))
    );
    return h('div', { class: 'add-layout', 'data-scroll-key': 'add-layout' }, form, renderEntryRail());
  }
  function renderEntryRail() {
    const proposed = state.form ? state.form.proposed : {};
    const values = SCORE_KEYS.map(key => scoreLabel(proposed[key])).join(' · ');
    const candidates = state.duplicateCandidates || [];
    return h('aside', { class: 'entry-rail' }, h('div', { class: 'panel-title' }, 'LLM DRAFT'), h('div', { class: 'draft-values' }, values), state.form && state.form.proposed_reason ? h('div', { class: 'draft-values draft-reason' }, `reason: ${state.form.proposed_reason}`) : h('div', { class: 'draft-values draft-reason' }, 'No proposed scores.'), h('button', { class: 'btn btn-primary', type: 'button', onclick: acceptDraft, disabled: !SCORE_KEYS.some(key => proposed[key] != null) }, 'ACCEPT DRAFT SCORES'), h('div', { class: 'rail-rule' }), h('div', { class: 'panel-title' }, 'NEAR-DUPLICATES'), state.duplicateLoading ? h('div', { class: 'duplicate-row' }, 'checking…') : candidates.length ? candidates.map(candidate => h('div', { class: 'duplicate-row' }, h('button', { type: 'button', onclick: () => navigate('library', candidate.id) }, `${fmt(candidate.similarity)} — ${fmt(candidate.title)}`))) : h('div', { class: 'duplicate-row' }, 'No candidates.'), state.form && state.form.id ? h('div', { class: 'rail-rule' }) : null, state.form && state.form.id ? h('div', { class: 'draft-values draft-reason' }, `editing ${state.form.id}`) : null);
  }

  function renderExport() {
    const e = state.export;
    const scopes = [['query', `current query (${state.total})`], ['all', `whole library (${state.libraryTotal || 0})`], ['selection', `selection (${state.selection.size})`]];
    const formats = [['bibtex', 'BIBTEX'], ['json', 'JSON'], ['csv', 'CSV']];
    const includeItems = [['scores', 'my scores as keywords'], ['notes', 'notes as annotations'], ['code', 'code / data URLs'], ['local_paths', 'local pdf paths']];
    return h('div', { class: 'export-layout', 'data-scroll-key': 'export-layout' },
      h('section', { class: 'export-options' },
        h('div', { class: 'panel-title' }, 'SCOPE'), h('div', { class: 'scope-list' }, ...scopes.map(([value, label]) => h('label', null, h('input', { type: 'radio', name: 'export-scope', checked: e.scope === value, onchange: () => { e.scope = value; scheduleExport(); } }), label))),
        h('div', { class: 'rail-rule' }), h('div', { class: 'panel-title' }, 'FORMAT'), h('div', { class: 'format-tabs' }, ...formats.map(([value, label]) => h('button', { class: `format-tab${e.format === value ? ' active' : ''}`, type: 'button', onclick: () => { e.format = value; scheduleExport(); } }, label))),
        h('div', { class: 'rail-rule' }), h('div', { class: 'panel-title' }, 'INCLUDE'), h('div', { class: 'include-list' }, ...includeItems.map(([key, label]) => h('label', null, h('input', { type: 'checkbox', checked: Boolean(e.include[key]), onchange: event => { e.include[key] = event.target.checked; scheduleExport(); } }), label))),
        h('div', { class: 'export-actions' }, h('button', { class: 'btn btn-primary', type: 'button', disabled: e.loading || !e.preview, onclick: downloadExport }, `DOWNLOAD .${e.format === 'bibtex' ? 'bib' : e.format}`), h('button', { class: 'btn', type: 'button', disabled: !e.preview, onclick: copyExport }, 'COPY')),
        e.error ? h('div', { class: 'status-line error-text', style: { marginTop: '7px' } }, e.error) : h('div', { class: 'status-line', style: { marginTop: '7px' } }, e.loading ? 'Generating…' : `${e.count || 0} rows · ${e.filename || ''}`)
      ),
      h('pre', { class: `preview${e.preview ? '' : ' preview-empty'}` }, e.preview || (e.loading ? 'Generating export…' : 'Choose a scope and format to preview the export.'))
    );
  }

  function syntaxHelp() {
    return 'term · field:value · tag:value · venue:value · author:value · year:2025\nrel:>7 · cred:>=5 · qual:6 · reimpl:10 · year:<2020\npdf:"posterior collapse" · state:unreviewed · has:code · has:pdf · is:starred\nA AND B · A OR B · NOT A · (A OR B) · implicit AND\nsort:rel · sort:-added';
  }
  function paletteItems() {
    const items = [
      ['Library', () => navigate('library')], ['Stats', () => navigate('stats')], ['Add entry', () => navigate('add')], ['Export', () => navigate('export')],
      ['Cycle table view', () => setView(state.view + 1)], ['Toggle SQL console', () => { state.showConsole = !state.showConsole; render(); }], ['Clear facets', clearFacets]
    ];
    const filter = state.paletteFilter.trim().toLowerCase();
    return items.filter(([label]) => !filter || label.toLowerCase().includes(filter)).map(([label, action]) => ({ label, action }));
  }
  function renderPalette() {
    if (!state.palette) return null;
    const items = paletteItems();
    const backdrop = h('div', { class: 'palette-backdrop', onclick: event => { if (event.target === event.currentTarget) { state.palette = false; render(); } } });
    const paletteInput = h('input', {
      id: 'palette-input', type: 'search', value: state.paletteFilter,
      placeholder: 'Jump, run, or search syntax…', ariaLabel: 'Command palette',
      oninput: event => { state.paletteFilter = event.target.value; state.paletteIndex = 0; render(); },
      onkeydown: event => {
        const list = paletteItems();
        if (event.key === 'Escape') { state.palette = false; render(); }
        else if (event.key === 'ArrowDown') { event.preventDefault(); state.paletteIndex = Math.min(Math.max(0, list.length - 1), state.paletteIndex + 1); render(); }
        else if (event.key === 'ArrowUp') { event.preventDefault(); state.paletteIndex = Math.max(0, state.paletteIndex - 1); render(); }
        else if (event.key === 'Enter' && list[state.paletteIndex]) { event.preventDefault(); state.palette = false; list[state.paletteIndex].action(); render(); }
      }
    });
    const search = h('div', { class: 'palette-search' }, h('span', null, '⌘K'), paletteInput);
    const actions = h('div', { class: 'palette-section' }, ...items.map((item, index) => h('button', { class: `palette-item${index === state.paletteIndex ? ' selected' : ''}`, type: 'button', onclick: () => { state.palette = false; item.action(); render(); } }, h('span', { class: 'palette-key' }, item.label), h('span', null, 'action'))));
    const savedRows = state.smartLists.length ? state.smartLists.map(list => h('div', { class: 'palette-list-row' }, h('button', { class: 'palette-item', type: 'button', onclick: () => { state.palette = false; if (list.kind === 'sql') { state.showConsole = true; state.sql.text = list.query || ''; navigate('library'); } else { state.q = list.query || ''; applySavedFacets(list.facets); state.offset = 0; navigate('library'); loadPapers(); } } }, h('span', { class: 'palette-key' }, list.name), h('span', null, list.kind || 'query')), h('button', { class: 'icon-button', type: 'button', ariaLabel: `Delete ${list.name}`, onclick: () => deleteSmartList(list) }, '×'))) : [h('div', { class: 'empty' }, 'No saved lists.')];
    const saved = h('div', { class: 'palette-section' }, h('div', { class: 'panel-title' }, 'SMART LISTS'), ...savedRows, h('div', { class: 'palette-save' }, h('input', { id: 'palette-list-name', 'data-focus-key': 'palette-list-name', class: 'input', value: state.paletteListName, placeholder: 'name current query', ariaLabel: 'Smart list name', oninput: event => { state.paletteListName = event.target.value; } }), h('button', { class: 'btn btn-primary', type: 'button', onclick: saveQueryList }, 'SAVE QUERY')));
    const help = h('div', { class: 'palette-section' }, h('div', { class: 'panel-title' }, 'QUERY SYNTAX'), h('div', { class: 'syntax' }, syntaxHelp()));
    backdrop.appendChild(h('div', { class: 'palette' }, search, actions, saved, help));
    return backdrop;
  }

  function renderScreen() {
    if (state.screen === 'stats') return renderStats();
    if (state.screen === 'add') return renderAdd();
    if (state.screen === 'export') return renderExport();
    return renderLibrary();
  }
  function render() {
    const restore = captureRenderState();
    const paletteHadFocus = Boolean(restore.focus && restore.focus.key === 'palette-input');
    document.querySelectorAll('.palette-backdrop').forEach(node => node.remove());
    if (state.booting) { clear(root); root.appendChild(h('div', { class: 'loading-state' }, 'Loading Corpus…')); return; }
    if (state.config.auth_required && (!state.session || !state.session.authenticated)) { renderGate(); return; }
    clear(root);
    root.appendChild(renderHeader());
    root.appendChild(renderScreen());
    const palette = renderPalette();
    if (palette) document.body.appendChild(palette);
    restoreRenderState(restore);
    // Focus synchronously: a deferred select() can arrive between the first
    // and second keystrokes when the palette is opened with ⌘K.
    const input = document.getElementById('palette-input');
    if (input && !paletteHadFocus) { input.focus({ preventScroll: true }); input.select(); }
  }

  function renderGate() {
    clear(root);
    const form = h('form', { class: 'gate-form', onsubmit: async event => { event.preventDefault(); const input = form.querySelector('input'); const button = form.querySelector('button'); button.disabled = true; try { await api('/api/login', { method: 'POST', body: { password: input.value } }); state.session = { authenticated: true }; state.booting = false; activateRoute(routeFromLocation()); loadSmartLists(); } catch (error) { toast(getError(error), true); button.disabled = false; } } }, h('div', { class: 'gate-brand' }, 'Corpus'), h('div', { class: 'gate-copy' }, 'Sign in to the literature review store.'), h('label', { for: 'password' }, 'PASSWORD'), h('input', { class: 'input', id: 'password', type: 'password', autocomplete: 'current-password', required: true }), h('button', { class: 'btn btn-primary', type: 'submit' }, 'ENTER'));
    root.appendChild(h('main', { class: 'gate' }, form));
    window.requestAnimationFrame(() => { const input = document.getElementById('password'); if (input) input.focus(); });
  }
  async function logout() {
    try { await api('/api/logout', { method: 'POST' }); } catch (_) {}
    state.session = { authenticated: false };
    render();
  }

  function moveSelection(delta) {
    if (!state.papers.length) return;
    let index = state.papers.findIndex(paper => String(paper.id) === String(state.selectedId));
    index = index < 0 ? 0 : Math.max(0, Math.min(state.papers.length - 1, index + delta));
    state.selectedId = state.papers[index].id;
    loadPaper(state.selectedId);
    render();
    const row = document.querySelector(`.paper-row:nth-child(${index + 2})`);
    if (row) row.focus();
  }
  function globalKeydown(event) {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); state.palette = !state.palette; state.paletteFilter = ''; state.paletteIndex = 0; render(); return; }
    if (state.palette) return;
    const target = event.target;
    const editing = target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName);
    if (state.screen === 'add' && !editing && /^[0-9]$/.test(event.key) && state.form) {
      const key = state.form.activeScore || SCORE_KEYS[0];
      state.form.scores[key] = Number(event.key);
      render();
      return;
    }
    if (editing) return;
    if (state.screen !== 'library') return;
    if (event.key === 'j') { event.preventDefault(); moveSelection(1); }
    else if (event.key === 'k') { event.preventDefault(); moveSelection(-1); }
    else if (event.key === 'x' && state.selectedId) { event.preventDefault(); selectPaper(state.selectedId, true); }
    else if (event.key === 'Enter' && state.selectedId) { event.preventDefault(); const paper = state.selectedDetail || state.papers.find(row => String(row.id) === String(state.selectedId)); if (event.shiftKey) openPdf(paper); else openCanonical(paper); }
  }

  async function bootstrap() {
    const route = routeFromLocation();
    state.screen = route.screen;
    state.routePaperId = route.paperId;
    try {
      const [config, session] = await Promise.all([api('/api/config'), api('/api/session')]);
      state.config = { fields: Array.isArray(config && config.fields) ? config.fields : [], auth_required: Boolean(config && config.auth_required) };
      state.session = session || { authenticated: !state.config.auth_required };
      state.booting = false;
      render();
      if (state.config.auth_required && !state.session.authenticated) return;
      loadSmartLists();
      // The header's ingest summary is useful on every screen, including the
      // initial library route, so fetch its shared source immediately.
      loadStats();
      activateRoute(route);
    } catch (error) {
      state.booting = false;
      state.bootError = getError(error);
      state.config.auth_required = false;
      render();
      toast(state.bootError, true);
    }
  }

  window.addEventListener('popstate', () => activateRoute(routeFromLocation()));
  document.addEventListener('keydown', globalKeydown);
  bootstrap();
})();
