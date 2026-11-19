#!/usr/bin/env node
// Validates manifest.json for Safari and flags API use Safari lacks.
// Usage: node scripts/validate.js   (exit 1 on errors; warnings do not fail)
'use strict';
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const errors = [];
const warnings = [];
const err = (m) => errors.push(m);
const warn = (m) => warnings.push(m);

// Permissions Safari does not implement (or ignores). Based on Apple's
// "Assessing your Safari web extension's browser compatibility" and MDN data;
// re-check against current docs, support moves with each Safari release.
const UNSUPPORTED_PERMISSIONS = new Set([
  'sidePanel', 'offscreen', 'identity', 'identity.email', 'tabGroups', 'debugger',
  'downloads', 'webRequestBlocking', 'declarativeNetRequestFeedback', 'proxy',
  'history', 'topSites', 'bookmarks', 'sessions', 'search', 'browsingData',
  'contentSettings', 'userScripts', 'system.cpu', 'system.memory', 'system.storage',
  'tts', 'pageCapture', 'tabCapture', 'desktopCapture', 'gcm', 'management',
]);

// chrome./browser. APIs known to be missing or unreliable in Safari.
const UNSUPPORTED_APIS = [
  'sidePanel', 'offscreen', 'identity', 'tabGroups', 'debugger', 'downloads',
  'history', 'bookmarks', 'sessions', 'search', 'browsingData', 'proxy',
  'userScripts', 'declarativeContent', 'storage.session.setAccessLevel',
  'runtime.getContexts', 'action.openPopup', 'runtime.onUserScriptMessage',
  'contextMenus.onShown', 'tabs.captureVisibleTab', 'windows.onBoundsChanged',
];

function readJson(file) {
  try { return JSON.parse(fs.readFileSync(path.join(root, file), 'utf8')); }
  catch (e) { err(`${file}: cannot read/parse (${e.message})`); return null; }
}

const manifest = readJson('manifest.json');
if (manifest) {
  if (manifest.manifest_version !== 3) err('manifest_version must be 3');
  for (const k of ['name', 'version', 'description']) if (!manifest[k]) err(`manifest.${k} is required`);
  if (manifest.version && !/^\d+(\.\d+){0,3}$/.test(manifest.version)) err('version must be 1–4 dot-separated integers');

  const bg = manifest.background;
  if (!bg) warn('no background script; the popup must do all network work');
  else {
    if (bg.service_worker && !bg.scripts) warn('background.service_worker only: Safari prefers background.scripts with "persistent": false');
    if (bg.service_worker && bg.scripts) warn('both service_worker and scripts set; Safari uses scripts, other browsers may complain');
    if (bg.persistent === true) warn('persistent background is deprecated in Safari MV3; use "persistent": false');
    if (bg.scripts && bg.persistent !== false) warn('background.scripts without "persistent": false');
    if (bg.type === 'module') warn('ES-module background scripts are not reliable in Safari');
  }

  const perms = [...(manifest.permissions || []), ...(manifest.optional_permissions || [])];
  for (const p of perms) if (UNSUPPORTED_PERMISSIONS.has(p)) err(`permission "${p}" is not supported in Safari`);
  if (manifest.host_permissions && manifest.host_permissions.some((h) => /<all_urls>|\*:\/\/\*\/\*/.test(h)))
    warn('broad host_permissions: Safari asks the user per site and App Review may question it; prefer optional_host_permissions');
  if (!perms.includes('activeTab') && !(manifest.host_permissions || []).length && !(manifest.optional_host_permissions || []).length)
    warn('no activeTab or host permissions; page access will fail');
  if (manifest.permissions && manifest.permissions.includes('scripting') && !perms.includes('activeTab') && !manifest.host_permissions)
    warn('scripting without activeTab/host permissions cannot inject');
  if (manifest.optional_permissions && manifest.optional_permissions.some((p) => /^https?:|<all_urls>/.test(p)))
    err('host patterns belong in optional_host_permissions in MV3');
  if (manifest.web_accessible_resources && manifest.web_accessible_resources.some((r) => typeof r === 'string'))
    err('MV3 web_accessible_resources must be objects with resources/matches');
  for (const k of ['browser_action', 'page_action']) if (manifest[k]) err(`${k} is MV2; use "action"`);
  if (manifest.content_security_policy && typeof manifest.content_security_policy === 'string') err('MV3 content_security_policy must be an object');
  if (manifest.browser_specific_settings) warn('browser_specific_settings is Firefox-only; Safari ignores it');
  if (manifest.options_page) warn('use options_ui; options_page support in Safari is limited');
  if (manifest.options_ui && manifest.options_ui.open_in_tab === false) warn('Safari opens options in a tab regardless of open_in_tab');

  // Referenced files must exist.
  const files = [];
  if (bg && bg.scripts) files.push(...bg.scripts);
  if (bg && bg.service_worker) files.push(bg.service_worker);
  if (manifest.action && manifest.action.default_popup) files.push(manifest.action.default_popup);
  if (manifest.options_ui && manifest.options_ui.page) files.push(manifest.options_ui.page);
  for (const cs of manifest.content_scripts || []) files.push(...(cs.js || []), ...(cs.css || []));
  files.push(...Object.values(manifest.icons || {}), ...Object.values((manifest.action || {}).default_icon || {}));
  for (const f of files) if (!fs.existsSync(path.join(root, f))) err(`referenced file missing: ${f}`);
}

// Scan JS for unsupported API use.
function walk(dir, out = []) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (['node_modules', 'test', 'scripts', 'build'].includes(e.name) && dir === root) continue;
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, out); else if (/\.(js|mjs|html)$/.test(e.name)) out.push(p);
  }
  return out;
}
for (const file of walk(root)) {
  const rel = path.relative(root, file);
  const lines = fs.readFileSync(file, 'utf8').split('\n');
  lines.forEach((line, i) => {
    const code = line.replace(/\/\/.*$/, '');
    for (const api of UNSUPPORTED_APIS) {
      if (new RegExp(`\\b(?:chrome|browser)\\.${api.replace(/\./g, '\\.')}\\b`).test(code)) err(`${rel}:${i + 1}: uses ${api}, unsupported in Safari`);
    }
    if (/\bimportScripts\s*\(/.test(code)) warn(`${rel}:${i + 1}: importScripts is service-worker only; list scripts in background.scripts`);
    if (/\bchrome\.(?!runtime\.lastError)/.test(code) && !/shim\.js$/.test(rel)) warn(`${rel}:${i + 1}: uses chrome.* callbacks; prefer browser.* (promises)`);
    if (/\beval\s*\(|new Function\s*\(/.test(code)) err(`${rel}:${i + 1}: eval/new Function is blocked by extension CSP`);
    if (/<script(?![^>]*\bsrc=)[^>]*>/.test(code) && rel.endsWith('.html')) err(`${rel}:${i + 1}: inline scripts are blocked by extension CSP`);
  });
}

for (const w of warnings) console.log('warn  ' + w);
for (const e of errors) console.log('error ' + e);
console.log(`${errors.length} error(s), ${warnings.length} warning(s)`);
process.exit(errors.length ? 1 : 0);
