// Casa Browser's pages: a confirmation before a form that ends something (data-confirm: the
// question; data-confirm-body, data-ok, data-cancel: the rest of the dialog). A Home Assistant style
// dialog where the browser has <dialog>, the browser's own confirm() where it has not.
document.querySelectorAll('form[data-confirm]').forEach(f => {
  let sure = false;
  f.addEventListener('submit', ev => {
    if (sure) return;
    ev.preventDefault();
    const d = document.createElement('dialog');
    if (typeof d.showModal !== 'function') {
      if (confirm(f.dataset.confirm)) { sure = true; f.requestSubmit(); }
      return;
    }
    d.className = 'confirm';
    const h = document.createElement('h2'); h.textContent = f.dataset.confirm; d.appendChild(h);
    if (f.dataset.confirmBody) {
      const p = document.createElement('p'); p.textContent = f.dataset.confirmBody; d.appendChild(p);
    }
    const row = document.createElement('div'); row.className = 'end';
    const no = document.createElement('button'); no.type = 'button'; no.className = 'plain';
    no.textContent = f.dataset.cancel || 'Cancel';
    const yes = document.createElement('button'); yes.type = 'button';
    yes.className = f.dataset.danger !== undefined ? 'danger fill' : 'primary';
    yes.textContent = f.dataset.ok || 'OK';
    row.append(no, yes); d.appendChild(row);
    no.addEventListener('click', () => d.close());
    yes.addEventListener('click', () => { sure = true; d.close(); f.requestSubmit(); });
    d.addEventListener('close', () => d.remove());
    document.body.appendChild(d);
    d.showModal();
  });
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
