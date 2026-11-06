// Casa Browser's pages: a confirmation before a form that ends something (data-confirm).
document.querySelectorAll('form[data-confirm]').forEach(f => {
  f.addEventListener('submit', ev => { if (!confirm(f.dataset.confirm)) ev.preventDefault(); });
});
// Open takes a while (the page, the capture): the button says so and cannot be pressed again, so
// a second tap cannot spend the form. A page brought back from the browser's history is usable.
document.querySelectorAll('button[data-busy]').forEach(b => {
  const was = b.textContent;
  b.form.addEventListener('submit', () => { b.disabled = true; b.textContent = b.dataset.busy; });
  addEventListener('pageshow', ev => { if (ev.persisted) { b.disabled = false; b.textContent = was; } });
});
// One tap copies an address or code (casa-test F17): the clipboard where the page is secure,
// else the selection's copy (Home Assistant over plain http); the code says so briefly.
document.querySelectorAll('code.copy').forEach(c => {
  const copy = async () => {
    const text = c.dataset.copy || c.textContent;
    try { await navigator.clipboard.writeText(text); }
    catch (err) {
      const ta = document.createElement('textarea'); ta.value = text; ta.setAttribute('readonly', '');
      ta.style.position = 'fixed'; ta.style.opacity = '0'; document.body.appendChild(ta);
      ta.select(); document.execCommand('copy'); ta.remove();
    }
    c.classList.add('copied'); c.dataset.was = c.dataset.was || c.textContent;
    c.textContent = c.dataset.copied || 'Copied';
    setTimeout(() => { c.textContent = c.dataset.was; c.classList.remove('copied'); }, 1200);
  };
  c.addEventListener('click', copy);
  c.addEventListener('keydown', ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); copy(); } });
});
// Home reloads itself when what it shows changes — a live view waiting, a question (casa-test
// F40: a pull-to-refresh in the Companion app did not reload this frame): a small check every
// few seconds while the page is visible, and at once when it comes back.
{
  const stamp = document.getElementById('stamp');
  if (stamp) {
    let leaving = false;     // a form sent (Open, a decision) or the page going: no reload over it
    addEventListener('submit', () => { leaving = true; }, true);
    addEventListener('pagehide', () => { leaving = true; });
    const check = async () => {
      if (leaving || document.visibilityState !== 'visible') return;
      try {
        const r = await fetch(stamp.dataset.url, {credentials: 'same-origin', cache: 'no-store'});
        const {stamp: now} = await r.json();
        if (now && now !== stamp.dataset.stamp && !leaving) location.reload();
      } catch (err) { /* offline: try again later */ }
    };
    setInterval(check, 3000);
    document.addEventListener('visibilitychange', check);
    addEventListener('pageshow', check);
    addEventListener('focus', check);
  }
}
