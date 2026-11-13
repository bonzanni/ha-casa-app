// Casa Browser's pages take Home Assistant's look: the panel is a frame on Home Assistant's own
// origin, so this reads the theme values Home Assistant's page resolved (the user's theme, light
// or dark) and sets them on this page. It only reads. Where the frame cannot see its parent (the
// page opened on its own), the stylesheet's Home Assistant defaults stay.
(() => {
  const NAMES = ['--primary-color', '--accent-color', '--primary-text-color', '--secondary-text-color',
    '--text-primary-color', '--primary-background-color', '--secondary-background-color',
    '--card-background-color', '--divider-color', '--success-color', '--warning-color',
    '--error-color', '--info-color', '--ha-font-family-body', '--ha-card-background'];
  const read = () => {
    let doc;
    try { doc = window.parent !== window && window.parent.document; } catch (err) { return null; }
    if (!doc) return null;
    // Home Assistant sets a theme's values on its root element or on <home-assistant>
    const els = [doc.documentElement, doc.querySelector('home-assistant')].filter(Boolean);
    const out = {};
    for (const name of NAMES) {
      for (const el of els) {
        const v = getComputedStyle(el).getPropertyValue(name).trim();
        if (v) { out[name] = v; }
      }
    }
    return out;
  };
  const apply = () => {
    const vals = read();
    if (!vals || !vals['--primary-background-color']) return;
    const root = document.documentElement.style;
    for (const [name, v] of Object.entries(vals)) root.setProperty(name, v);
    if (vals['--ha-card-background']) root.setProperty('--card-background-color', vals['--ha-card-background']);
    // the scrollbars and form controls follow the theme's lightness, not the phone's setting
    const probe = document.createElement('i');
    probe.style.setProperty('background-color', 'var(--primary-background-color)');
    document.documentElement.appendChild(probe);
    const rgb = (getComputedStyle(probe).backgroundColor.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
    probe.remove();
    if (rgb.length === 3) root.setProperty('color-scheme', (rgb[0] * 299 + rgb[1] * 587 + rgb[2] * 114) / 1000 < 128 ? 'dark' : 'light');
  };
  apply();
  // a theme or dark-mode change in Home Assistant while the panel is open
  try {
    const doc = window.parent !== window && window.parent.document;
    if (doc) {
      const targets = [doc.documentElement, doc.querySelector('home-assistant')].filter(Boolean);
      const mo = new MutationObserver(apply);
      for (const t of targets) mo.observe(t, {attributes: true, attributeFilter: ['style', 'class']});
    }
  } catch (err) { /* not Home Assistant's frame */ }
})();
