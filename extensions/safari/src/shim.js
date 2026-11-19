// Safari exposes `browser.*` (promise based). Alias it to `chrome.*` for code
// written against either name, and the reverse if only `chrome` exists.
(function (g) {
  if (typeof g.browser === 'undefined' && typeof g.chrome !== 'undefined') g.browser = g.chrome;
  if (typeof g.chrome === 'undefined' && typeof g.browser !== 'undefined') g.chrome = g.browser;
})(typeof globalThis !== 'undefined' ? globalThis : self);
