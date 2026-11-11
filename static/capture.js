/* Interactive capture stays separate from the detailed paper editor. */
(function () {
  'use strict';
  window.CorpusCapture = function ({ h, api, render, toast, fields, projectSymbol, currentProjects, onAdded, openPaper, openInbox, onProjects }) {
    const c = { mode: 'search', query: '', input: '', searchIn: 'all', projects: [], initialized: false, expanded: null,
      items: [], selected: new Set(), focused: null, offset: 0, states: {}, errors: [], busy: false,
      projectsOpen: false, generation: 0, controller: null, timer: null, lastArxiv: 0, more: false, fallbackTitle: '' };
    const label = id => (fields().find(field => field.id === id) || {}).label || id;
    const key = paper => paper.arxiv_id ? `arxiv:${paper.arxiv_id}` : paper.doi ? `doi:${paper.doi.toLowerCase()}` : `title:${paper.title.toLowerCase().replace(/[^\p{L}\p{N}]/gu, '')}`;
    const validProjects = values => Array.isArray(values) ? [...new Set(values.filter(id => fields().some(field => field.id === id)))] : [];
    function initialize() {
      if (c.initialized) return;
      let remembered = [];
      try { remembered = validProjects(JSON.parse(localStorage.getItem('corpus.capture.projects') || '[]')); } catch (_) {}
      c.projects = validProjects(currentProjects());
      if (!c.projects.length) c.projects = remembered;
      if (!c.projects.length && fields().length) c.projects = [fields()[0].id];
      c.initialized = true;
    }
    function project(id, checked) {
      c.projects = checked ? [...new Set([...c.projects, id])] : c.projects.filter(value => value !== id);
      try { localStorage.setItem('corpus.capture.projects', JSON.stringify(c.projects)); } catch (_) {}
      onProjects(c.projects); render();
    }
    function setProjects(values) {
      c.projects = validProjects(values);
      try { localStorage.setItem('corpus.capture.projects', JSON.stringify(c.projects)); } catch (_) {}
      onProjects(c.projects); render();
    }
    function cancel() {
      ++c.generation;
      window.clearTimeout(c.timer);
      if (c.controller) c.controller.abort();
      c.states = {};
    }
    function activate(route) {
      initialize();
      if (route.captureUrl) {
        c.mode = 'search'; c.query = route.captureUrl; c.fallbackTitle = route.captureTitle || '';
        window.setTimeout(() => search(), 0);
        window.opener = null;
      }
      window.setTimeout(() => { const input = document.getElementById('capture-query'); if (input) input.focus(); }, 0);
    }
    function merge(papers, select = false) {
      papers.forEach(paper => {
        const normalize = value => String(value || '').toLowerCase().replace(/[^\p{L}\p{N}]/gu, '');
        const identity = key(paper), previous = c.items.find(item => item.key === identity || (paper.doi && item.paper.doi && paper.doi.toLowerCase() === item.paper.doi.toLowerCase()) ||
          (normalize(paper.title).length >= 20 && normalize(paper.title) === normalize(item.paper.title) && paper.authors && paper.authors[0] && normalize(paper.authors[0]) === normalize((item.paper.authors || [])[0])));
        if (previous) {
          if (!previous.paper.abstract && paper.abstract) previous.paper.abstract = paper.abstract;
          if (!previous.paper.doi && paper.doi) previous.paper.doi = paper.doi;
          if (!previous.paper.arxiv_id && paper.arxiv_id) previous.paper.arxiv_id = paper.arxiv_id;
          (paper.artifacts || []).forEach(artifact => { if (!(previous.paper.artifacts || []).some(old => old.kind === artifact.kind && old.url === artifact.url)) { previous.paper.artifacts = [...(previous.paper.artifacts || []), artifact]; } });
          if (!previous.paper.existing && paper.existing) previous.paper.existing = paper.existing;
          return;
        }
        c.items.push({ key: identity, paper, result: null });
        if (select && !(paper.existing && paper.existing.admission !== 'approved')) c.selected.add(identity);
      });
      if (!c.focused && c.items.length) c.focused = c.items[0].key;
    }
    async function resolve(identifiers, signal, generation) {
      const response = await api('/api/catalog/resolve', { method: 'POST', signal, body: { identifiers } });
      const retry = response.results.map((result, index) => result.retry_after ? index : -1).filter(index => index >= 0);
      if (retry.length) {
        await new Promise(done => window.setTimeout(done, 3100));
        if (generation !== c.generation) return response;
        const again = await api('/api/catalog/resolve', { method: 'POST', signal, body: { identifiers: retry.map(index => identifiers[index]) } });
        again.results.forEach((result, index) => { response.results[retry[index]] = result; });
      }
      return response;
    }
    async function search(more = false) {
      initialize(); cancel();
      const query = c.query.trim();
      if (query.length < 2) { c.items = []; render(); return; }
      const generation = c.generation;
      c.controller = new AbortController();
      const signal = c.controller.signal;
      c.errors = [];
      if (!more) { c.items = []; c.selected.clear(); c.focused = null; c.offset = 0; }
      else c.offset += 20;
      const identifier = /^(?:https?:\/\/|(?:doi:\s*)?10\.\d{4,9}\/|(?:arxiv:\s*)?\d{4}\.\d{4,5}(?:v\d+)?$|[a-z.-]+\/\d{7})/i.test(query);
      if (identifier) {
        c.states = { metadata: 'loading' }; c.more = false; render();
        try {
          const response = await resolve([query], signal, generation);
          if (generation !== c.generation) return;
          const result = response.results[0];
          if (result.paper) merge([result.paper]);
          else if (c.fallbackTitle) {
            c.query = c.fallbackTitle.replace(/\s+[|–—]\s+.*$/, '').slice(0, 500);
            c.fallbackTitle = ''; search(); return;
          } else c.errors.push(result.error || 'No matching paper found. Try its title instead.');
        } catch (error) { if (generation === c.generation && error.name !== 'AbortError') c.errors.push(error.message); }
        finally { if (generation === c.generation) { c.states = {}; render(); } }
        return;
      }
      c.states = { library: 'loading', crossref: 'loading', arxiv: 'loading' }; c.more = false; render();
      async function provider(source, retry = false) {
        if (source === 'arxiv') {
          const wait = Math.max(0, 3100 - (Date.now() - c.lastArxiv));
          if (wait) await new Promise(resolve => window.setTimeout(resolve, wait));
          if (generation !== c.generation) return;
          c.lastArxiv = Date.now();
        }
        try {
          const params = new URLSearchParams({ q: query, source, mode: c.searchIn, offset: String(c.offset) });
          const response = await api(`/api/catalog/search?${params}`, { signal });
          if (generation !== c.generation) return;
          merge(response.papers); c.more = c.more || (response.has_more && c.offset < 200); c.states[source] = 'done';
        } catch (error) {
          if (generation !== c.generation || error.name === 'AbortError') return;
          if (source === 'arxiv' && error.status === 429 && !retry) {
            await new Promise(resolve => window.setTimeout(resolve, 3100));
            if (generation === c.generation) return provider(source, true);
            return;
          }
          c.states[source] = 'unavailable';
        }
        if (generation === c.generation) render();
      }
      await Promise.allSettled([provider('library'), provider('crossref'), provider('arxiv')]);
    }
    function schedule(value) {
      c.query = value; cancel();
      if (value.trim().length >= 3) c.timer = window.setTimeout(() => search(), 400);
      else { c.items = []; c.errors = []; render(); }
    }
    async function resolveLinks() {
      cancel();
      const values = [...new Set(c.input.split(/\r?\n/).map(value => value.trim()).filter(Boolean))];
      if (!values.length || values.length > 30) { c.errors = ['Paste 1–30 links or identifiers, one per line.']; render(); return; }
      const generation = c.generation;
      c.controller = new AbortController();
      c.items = []; c.selected.clear(); c.focused = null; c.errors = []; c.states = { metadata: 'loading' }; render();
      try {
        const response = await resolve(values, c.controller.signal, generation);
        if (generation !== c.generation) return;
        response.results.forEach(result => {
          if (result.paper) merge([result.paper], true);
          else c.errors.push(`${result.input}: ${result.error}`);
        });
      } catch (error) { if (generation === c.generation && error.name !== 'AbortError') c.errors.push(error.message); }
      finally { if (generation === c.generation) { c.states = {}; render(); } }
    }
    const loading = () => Object.values(c.states).some(value => value === 'loading');
    const eligible = item => !(item.paper.existing && item.paper.existing.admission !== 'approved') && !(item.result && ['similar', 'needs_review'].includes(item.result.status));
    const alreadyFiled = item => item.paper.existing && item.paper.existing.admission === 'approved' && c.projects.length && c.projects.every(id => item.paper.existing.projects.includes(id));
    async function add(items, override = {}) {
      if (c.busy || !c.projects.length || !items.length) return;
      const projects = [...c.projects];
      c.busy = true; c.errors = []; render();
      let added = 0, filed = 0;
      try {
        for (let start = 0; start < items.length; start += 50) {
          const batch = items.slice(start, start + 50);
          const response = await api('/api/capture', { method: 'POST', body: { projects, query: c.mode === 'links' ? 'Pasted paper links' : c.query, papers: batch.map(item => ({ ...item.paper, ...(item.paper.existing ? { use_existing_id: item.paper.existing.id } : {}), ...override })) } });
          response.results.forEach(result => {
            const item = batch[result.index];
            item.result = result;
            if (result.paper) {
              item.paper.existing = { id: result.paper.id, admission: result.paper.admission, projects: result.paper.projects };
              c.selected.delete(item.key);
              if (result.status === 'added') ++added; else ++filed;
            }
            if (result.status === 'error') c.errors.push(`${item.paper.title}: ${result.error}`);
          });
        }
      } catch (error) { c.errors.push(error.message); }
      finally {
        if (added || filed) { onAdded(); toast(`${added ? `${added} paper${added === 1 ? '' : 's'} added` : ''}${added && filed ? ' · ' : ''}${filed ? `${filed} existing paper${filed === 1 ? '' : 's'} filed` : ''}`); }
        c.busy = false; render();
      }
    }
    function tabs() {
      return h('div', { class: 'capture-tabs' }, ...[['search', 'Search papers'], ['links', 'Paste links'], ['manual', 'Manual entry']].map(([mode, title]) => h('button', {
        class: 'toolbar-button', type: 'button', 'aria-pressed': String(c.mode === mode), onclick: () => { cancel(); c.mode = mode; c.errors = []; c.items = []; c.selected.clear(); c.focused = null; render(); }
      }, title)));
    }
    function bookmarklet() {
      return `javascript:(()=>{const u=new URL(${JSON.stringify(window.location.origin)});u.searchParams.set('screen','add');u.searchParams.set('url',location.href);u.searchParams.set('title',document.title);window.open(u.href,'corpus-capture','width=1100,height=780');})()`;
    }
    function projectsRail() {
      return h('aside', { class: 'facet-rail capture-projects', ariaLabel: 'Projects to add papers to' },
        h('section', { class: 'facet-section' }, h('div', { class: 'facet-title' }, 'ADD TO PROJECTS'),
          ...fields().map(field => h('label', { class: 'facet-option' }, h('input', { type: 'checkbox', checked: c.projects.includes(field.id), onchange: event => project(field.id, event.target.checked) }), h('span', { class: 'facet-label' }, projectSymbol(field.symbol), field.label))),
          h('p', { class: 'capture-hint' }, 'Your choices stay selected for the next paper.')),
        h('section', { class: 'facet-section' }, h('label', { class: 'facet-title', for: 'capture-search-in' }, 'SEARCH IN'), h('select', { id: 'capture-search-in', class: 'select', value: c.searchIn, onchange: event => { c.searchIn = event.target.value; if (c.query) search(); } },
          ...[['all','Titles & authors'],['title','Titles'],['author','Authors']].map(([value, title]) => h('option', { value, selected: c.searchIn === value }, title)))),
        h('section', { class: 'facet-section capture-browser-button' }, h('div', { class: 'facet-title' }, 'FROM A PAPER TAB'),
          h('a', { class: 'artifact-link', href: bookmarklet(), draggable: true, onclick: event => event.preventDefault(), title: 'Drag this link to your browser bookmarks bar' }, 'SAVE TO CORPUS'),
          h('p', { class: 'capture-hint' }, 'Drag this to your bookmarks bar. Click it on a paper page to open its Add view.')));
    }
    function resultActions(item) {
      const existing = item.paper.existing, result = item.result;
      if (result && result.status === 'similar') return h('div', { class: 'capture-similar' },
        h('span', null, `Possible duplicate: ${result.existing.title}`),
        h('button', { class: 'btn', type: 'button', disabled: c.busy, onclick: () => add([item], { use_existing_id: result.existing.id, admit_existing: result.existing.admission !== 'approved' }) }, result.existing.admission === 'approved' ? 'USE EXISTING' : result.existing.admission === 'dismissed' ? 'RESTORE EXISTING' : 'APPROVE EXISTING'),
        h('button', { class: 'btn', type: 'button', disabled: c.busy, onclick: () => add([item], { allow_similar: true }) }, 'KEEP SEPARATE'));
      if ((existing && existing.admission !== 'approved') || (result && result.status === 'needs_review')) {
        const target = existing || result.existing;
        return h('div', { class: 'capture-existing-actions' },
          h('button', { class: 'btn', type: 'button', disabled: c.busy || !c.projects.length, onclick: () => add([item], { admit_existing: true }) }, target.admission === 'dismissed' ? 'RESTORE & ADD' : 'APPROVE & ADD'),
          h('button', { class: 'btn btn-quiet', type: 'button', onclick: () => openInbox(target.id) }, 'VIEW INBOX'));
      }
      return h('button', { class: 'btn btn-primary capture-add-one', type: 'button', ariaLabel: `${alreadyFiled(item) ? 'Already filed' : 'Add'} ${item.paper.title}`, disabled: c.busy || !c.projects.length || alreadyFiled(item), onclick: () => add([item]) }, alreadyFiled(item) ? 'ADDED' : 'ADD');
    }
    function preview() {
      const item = c.items.find(row => row.key === c.focused);
      if (!item) return h('aside', { class: 'detail-pane capture-preview' }, h('div', { class: 'empty' }, 'Select a result to inspect its abstract.'));
      const paper = item.paper;
      return h('aside', { class: 'detail-pane capture-preview', 'data-scroll-key': 'capture-preview' },
        h('div', { class: 'detail-header' }, h('div', { class: 'detail-id' }, paper.arxiv_id || paper.doi || ''), h('div', { class: 'detail-title' }, paper.title), h('div', { class: 'detail-meta' }, `${(paper.authors || []).join('; ')} · ${paper.year || '—'}`)),
        h('section', { class: 'detail-section' }, h('div', { class: 'panel-title' }, 'ABSTRACT'), h('div', { class: 'detail-abstract' }, paper.abstract || 'No abstract supplied by this source.')),
        h('section', { class: 'detail-section' }, h('div', { class: 'artifact-list' }, paper.canonical_url ? h('a', { class: 'artifact-link', href: paper.canonical_url, target: '_blank', rel: 'noopener' }, 'SOURCE ↗') : null,
          paper.existing ? h('button', { class: 'artifact-link', type: 'button', onclick: () => paper.existing.admission === 'approved' ? openPaper(paper.existing.id) : openInbox(paper.existing.id) }, 'OPEN SAVED PAPER') : null)),
        paper.existing ? h('section', { class: 'detail-section' }, h('div', { class: 'panel-title' }, 'FILED IN'), h('div', { class: 'detail-abstract' }, paper.existing.projects.map(label).join(' · '))) : null);
    }
    function view() {
      initialize();
      const selected = c.items.filter(item => c.selected.has(item.key) && eligible(item) && !alreadyFiled(item));
      const rows = c.items.map(item => h('div', { class: `capture-result${c.focused === item.key ? ' selected' : ''}` },
        h('input', { type: 'checkbox', checked: c.selected.has(item.key), disabled: !eligible(item) || alreadyFiled(item) || c.busy, ariaLabel: `Select ${item.paper.title}`, onchange: event => { if (event.target.checked) c.selected.add(item.key); else c.selected.delete(item.key); render(); } }),
        h('button', { class: 'capture-result-info', type: 'button', ariaLabel: `Inspect ${item.paper.title}`, onclick: () => { c.focused = item.key; c.expanded = c.expanded === item.key ? null : item.key; render(); } }, h('span', { class: 'capture-result-title' }, item.paper.title),
          h('span', { class: 'capture-result-meta' }, `${(item.paper.authors || []).slice(0, 5).join('; ')}${(item.paper.authors || []).length > 5 ? '; …' : ''}`),
          h('span', { class: 'capture-result-meta' }, `${item.paper.year || '—'} · ${item.paper.venue || '—'}${item.paper.existing ? ` · ${item.paper.existing.admission === 'approved' ? 'In library' : item.paper.existing.admission === 'dismissed' ? 'Previously dismissed' : 'Awaiting approval'}` : ''}`)),
        h('div', { class: 'capture-result-actions' }, resultActions(item)),
        c.expanded === item.key ? h('div', { class: 'capture-mobile-abstract' }, item.paper.abstract || 'No abstract supplied by this source.') : null));
      const query = h('form', { class: 'query-toolbar', onsubmit: event => { event.preventDefault(); search(); } },
        h('div', { class: 'query-wrap' }, h('span', { class: 'query-prompt', ariaHidden: true }, '>'),
          h('input', { id: 'capture-query', class: 'query-input', type: 'search', value: c.query, placeholder: 'Title, author, DOI, or paper link…', ariaLabel: 'Find papers', maxlength: 3000, oninput: event => schedule(event.target.value) })),
        h('button', { class: 'toolbar-button capture-project-toggle', type: 'button', 'aria-expanded': String(c.projectsOpen), onclick: () => { c.projectsOpen = !c.projectsOpen; render(); } }, `PROJECTS (${c.projects.length})`),
        h('button', { class: 'toolbar-button', type: 'submit' }, 'SEARCH'));
      const links = c.mode === 'links' ? h('section', { class: 'capture-links' }, h('label', { class: 'panel-title', for: 'capture-links' }, 'PAPER LINKS · ONE PER LINE'),
        h('textarea', { id: 'capture-links', class: 'input', rows: 5, value: c.input, placeholder: 'https://arxiv.org/abs/…\nhttps://doi.org/…', oninput: event => { c.input = event.target.value; } }),
        h('div', { class: 'detail-actions' }, h('button', { class: 'btn btn-primary', type: 'button', disabled: loading(), onclick: resolveLinks }, loading() ? 'LOOKING UP…' : 'FIND PAPERS'), h('span', { class: 'capture-hint' }, 'Up to 30 links. Review the matches, then add them together.'))) : null;
      const status = Object.entries(c.states).map(([name, value]) => `${name === 'arxiv' ? 'arXiv' : name === 'crossref' ? 'Crossref' : name === 'library' ? 'Saved' : 'Links'} ${value === 'loading' ? 'searching…' : value === 'unavailable' ? 'unavailable' : 'ready'}`).join(' · ');
      const toolbar = c.mode === 'search' ? query : h('div', { class: 'query-toolbar' }, h('div', { class: 'query-wrap capture-hint' }, 'Paste links from your paper tabs'), h('button', { class: 'toolbar-button capture-project-toggle', type: 'button', onclick: () => { c.projectsOpen = !c.projectsOpen; render(); } }, `PROJECTS (${c.projects.length})`));
      return h('main', { class: `screen capture-screen${c.projectsOpen ? ' projects-open' : ''}`, onkeydown: event => {
        if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') { event.preventDefault(); const target = c.items.find(item => item.key === c.focused); add(selected.length ? selected : target && eligible(target) && !alreadyFiled(target) ? [target] : []); }
        if (event.key === 'Escape' && c.projectsOpen) { c.projectsOpen = false; render(); }
      } }, toolbar, tabs(),
        h('div', { class: 'capture-layout' }, projectsRail(), h('section', { class: 'table-region capture-results' }, links,
          h('div', { class: 'table-head capture-list-head' }, h('span', null, `${c.items.length} RESULTS`), h('span', { role: 'status' }, status)),
          h('div', { class: 'capture-result-list', 'data-scroll-key': 'capture-results' }, ...rows,
            !rows.length ? h('div', { class: 'empty' }, loading() ? 'Finding papers…' : c.mode === 'links' ? 'Paste paper links above, then choose Find papers.' : c.query ? 'No matches found. Try fewer title words or an author name.' : 'Search by title or author. Choose projects, then add a paper in one click.') : null,
            ...c.errors.map(error => h('div', { class: 'notice error-text', role: 'alert' }, error)),
            Object.values(c.states).includes('unavailable') ? h('div', { class: 'capture-retry' }, h('span', { class: 'capture-hint' }, 'One source could not respond. Other results remain available. '), h('button', { class: 'btn', type: 'button', onclick: () => search() }, 'RETRY SEARCH')) : null),
          h('footer', { class: 'capture-actionbar' }, h('div', { class: 'detail-actions' },
            h('button', { class: 'btn btn-primary', type: 'button', title: 'Ctrl/⌘ + Enter', disabled: c.busy || !c.projects.length || !selected.length, onclick: () => add(selected) }, c.busy ? 'ADDING…' : `ADD SELECTED (${selected.length})`),
            h('button', { class: 'btn', type: 'button', disabled: !rows.length || c.busy, onclick: () => { c.items.filter(item => eligible(item) && !alreadyFiled(item)).forEach(item => c.selected.add(item.key)); render(); } }, 'SELECT ALL'),
            c.more && c.mode === 'search' ? h('button', { class: 'btn', type: 'button', disabled: loading(), onclick: () => search(true) }, 'MORE RESULTS') : null),
            h('div', { class: 'capture-hint' }, c.projects.length ? `Add to: ${c.projects.map(label).join(' · ')}` : 'Choose at least one project.'))), preview()));
    }
    return { state: c, activate, cancel, render: view, tabs, setProjects, projects: () => { initialize(); return c.projects; } };
  };
})();
