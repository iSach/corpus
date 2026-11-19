// Apply the saved browser preference before the interface is painted.
try {
  const theme = localStorage.getItem('corpus.theme');
  if (['light', 'midnight', 'oled'].includes(theme)) document.documentElement.dataset.theme = theme;
} catch (_) {}
