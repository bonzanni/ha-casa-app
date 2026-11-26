// The page's script. It only makes the page quicker to use: everything it does, the page also
// does through its plain forms and boxes, and it never decides what a form saves. A hidden value
// it fetches is put on the page as text, and taken off again after 30 seconds, on Hide, or when
// the page goes out of sight; a Show or Copy not done 30 seconds after its tap is dropped. It
// keeps no value anywhere else. The sentences it shows that name the page's items come from the
// page (attributes of <body>), and so does the shape of its form, so any app's page can run it.
(() => {
  'use strict';
  const LIMIT = 30000;        // how long a Show or Copy may take, and how long a value stays shown
  const COPIED_FOR = 2500;    // how long a Copy button says it copied
  const DOTS = '•'.repeat(8);
  const HOLD = 'Press and hold the value to copy it.';
  const COPIED = 'Copied to clipboard.';
  const FAILED = 'Could not load this value. Reload the page and try again.';
  const TOO_LONG = 'This took too long. Reload the page and try again.';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  // the page's own sentences: notSaved, noValueLeft, countOne and countMany ({} is a number)
  const words = document.body.dataset;

  // A value block's line for what happened: copied, press and hold, or a failure.
  const say = (block, text) => {
    const line = $('.v-feedback', block);
    if (line) line.textContent = text;
  };

  // -- a value on its way. Every step that holds a value, or waits for one, is an operation of
  // its value block, and a block has at most one: starting a Show or a Copy first ends the one
  // before it, whose late answer or clipboard outcome then does nothing (only the fresh code an
  // answer brings is kept). An operation that fetches (a Show, or a Copy of a value not on the
  // page) expires LIMIT after its tap. The expiry is a time, checked by live() as well as by a
  // timer, so an answer handled late is dropped even before the timer has run; either way the
  // operation ends and the page says it took too long, once. Ending an operation empties it and
  // clears its timer. Hide, the page going out of sight and pagehide end it (hide()). After each
  // await an operation goes on only while live(): not ended, not expired, page in sight.
  // Otherwise it stops without touching the clipboard or the page.
  const pending = new Map();   // value block -> its operation still running
  const end = (op) => {
    op.over = true;
    op.value = null;
    clearTimeout(op.timer);
    if (pending.get(op.block) === op) pending.delete(op.block);
  };
  const expire = (op) => {
    if (op.over) return;
    end(op);
    if (document.visibilityState !== 'hidden') say(op.block, TOO_LONG);
  };
  const begin = (block, fetches) => {
    if (pending.has(block)) end(pending.get(block));
    const op = {block, value: null, over: false, until: null, timer: null};
    pending.set(block, op);
    if (fetches) {
      op.until = performance.now() + LIMIT;
      op.timer = setTimeout(() => expire(op), LIMIT);
    }
    return op;
  };
  const live = (op) => {
    if (op.until !== null && performance.now() >= op.until) expire(op);
    return !op.over && document.visibilityState !== 'hidden';
  };

  // Show and Copy ask the page for a value with the value's form, which spends that form's code
  // and gets the next one back. The code leaves the form as the request is made, so it is never
  // sent twice; the next one is installed whatever happens next, so the next tap works. Without
  // one (the answer was lost or refused) the form asks for a reload instead. While the request
  // is out the form is busy: its Show and Copy, which would need the code, are off until the
  // request ends, either way, so no tap on them is taken and then dropped (a Hide stays on).
  // The value goes into the operation only while it is live.
  const codeOf = (form) => form.querySelector('input[name="code"]');
  const busy = new WeakSet();
  const ask = async (form, op) => {
    const body = new URLSearchParams(new FormData(form));
    codeOf(form).value = '';
    busy.add(form);
    const off = $$('[data-action="show"], [data-action="copy"]', form)
      .filter((control) => control.textContent !== 'Hide');
    for (const control of off) control.disabled = true;
    form.setAttribute('aria-busy', 'true');
    try {
      const answer = await fetch(form.action, {
        method: 'POST',
        body,
        headers: {Accept: 'application/json'},
        credentials: 'same-origin',
        cache: 'no-store',
      });
      if (answer.status !== 200) throw new Error('not answered');
      const {value, code} = await answer.json();
      if (typeof code === 'string') codeOf(form).value = code;
      if (typeof value !== 'string' || typeof code !== 'string') throw new Error('not understood');
      if (live(op)) op.value = value;
    } finally {
      busy.delete(form);
      for (const control of off) control.disabled = false;
      form.removeAttribute('aria-busy');
    }
  };

  // A value put on the page has a deadline of its own, set once, LIMIT from the moment it is
  // put there: nothing moves it while the value stays (copying it, or a second display into the
  // same block), and hiding the value clears it, so it never reaches a later one.
  const shownUntil = new Map();   // a value block showing a value -> its display deadline

  const hide = (block) => {
    if (pending.has(block)) end(pending.get(block));
    clearTimeout(shownUntil.get(block));
    shownUntil.delete(block);
    if ($('.v-feedback', block) && $('.v-feedback', block).textContent === HOLD) say(block, '');
    const shown = $('.v-revealed', block);
    if (!shown) return;
    shown.textContent = '';
    shown.remove();
    const form = $('.v-reveal', block);
    const toggle = $('[data-action="show"]', block);
    if (toggle) {
      toggle.textContent = 'Show';
      toggle.setAttribute('aria-expanded', 'false');
    }
    const inline = $('[data-hide-inline]', block);
    if (inline) inline.remove();
    // a value card puts its dots back; a list row has none
    if (block.classList.contains('v-value-card') && form && !$('.v-dots', block)) {
      const dots = document.createElement('span');
      dots.className = 'v-dots';
      dots.setAttribute('aria-label', 'Hidden value');
      dots.textContent = DOTS;
      form.before(dots);
    }
    if ($('.v-dots', block)) $('.v-dots', block).hidden = false;
  };

  const display = (block) => {
    if (!shownUntil.has(block)) shownUntil.set(block, setTimeout(() => hide(block), LIMIT));
  };

  const show = (block, value) => {
    // an answer that arrives once the page is out of sight is not put on it
    if (document.visibilityState === 'hidden') return null;
    // a value already shown keeps its box and its deadline
    if ($('.v-revealed', block)) return $('.v-revealed', block);
    const shown = document.createElement('pre');
    shown.className = 'v-value v-revealed';
    if (block.classList.contains('v-list-item')) $('.v-feedback', block).before(shown);
    else $('.v-reveal', block).before(shown);
    // an empty value says so, as the page does
    if (value === '') {
      shown.textContent = 'Empty';
      shown.setAttribute('data-empty', '');
    } else {
      shown.textContent = value;
    }
    if ($('.v-dots', block)) $('.v-dots', block).hidden = true;
    const toggle = $('[data-action="show"]', block);
    if (toggle) {
      toggle.textContent = 'Hide';
      toggle.setAttribute('aria-expanded', 'true');
      toggle.disabled = false;
    } else if (!$('[data-hide-inline]', block)) {
      // a list row has no Show: it gets a Hide of its own under the value
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'v-button v-button--quiet';
      button.dataset.hideInline = '';
      button.textContent = 'Hide';
      button.addEventListener('click', () => hide(block));
      shown.after(button);
    }
    display(block);
    return shown;
  };

  const hideAll = () => { for (const block of $$('[data-value-block]')) hide(block); };
  addEventListener('pagehide', hideAll);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') hideAll();
  });

  const wireShow = (button) => {
    const form = button.form;
    const block = form.closest('[data-value-block]');
    button.setAttribute('aria-expanded', String(Boolean($('.v-revealed', block))));
    button.addEventListener('click', async (event) => {
      event.preventDefault();
      if ($('.v-revealed', block)) {
        hide(block);
        return;
      }
      if (busy.has(form)) return;
      if (!codeOf(form).value) {
        say(block, FAILED);
        return;
      }
      say(block, '');
      const op = begin(block, true);
      try {
        await ask(form, op);
        if (live(op)) show(block, op.value);
      } catch (err) {
        // the code may be spent: never send the form again on its own
        if (live(op)) say(block, FAILED);
      } finally {
        end(op);
      }
    });
  };

  const copied = (block, button) => {
    button.textContent = 'Copied';
    say(block, COPIED);
    setTimeout(() => {
      button.textContent = 'Copy';
      if ($('.v-feedback', block) && $('.v-feedback', block).textContent === COPIED) say(block, '');
    }, COPIED_FOR);
  };

  // The operation's value, at the moment it is handed over, or a refusal if it was hidden
  // meanwhile or is empty (there is nothing to copy; the value is shown as "Empty" instead)
  const handOver = (op) => {
    if (!live(op) || op.value === null) throw new Error('hidden meanwhile');
    if (op.value === '') throw new Error('empty');
    return op.value;
  };

  // Where the browser has ClipboardItem, the clipboard is called inside the tap with a promise
  // of the value still on its way (ASKED: the fetch), so that a browser that allows a copy only
  // during a tap still allows this one; with writeText alone, it is called once the value has
  // arrived. Nothing here keeps the value across an await: it is read from the operation when
  // it is handed over. True when copied; false where there is no clipboard (Home Assistant over
  // plain http) or it refused.
  const toClipboard = async (op, asked) => {
    const clipboard = navigator.clipboard;
    if (typeof ClipboardItem === 'function' && clipboard && clipboard.write) {
      try {
        await clipboard.write([new ClipboardItem({
          'text/plain': Promise.resolve(asked).then(() => new Blob([handOver(op)], {type: 'text/plain'})),
        })]);
        return true;
      } catch (err) { /* the plain way below */ }
    }
    if (!clipboard || !clipboard.writeText) return false;
    try {
      await asked;
      await clipboard.writeText(handOver(op));
      return true;
    } catch (err) {
      return false;
    }
  };

  // Copy a value already on the page (NODE); when the clipboard refuses, the line on pressing
  // and holding. A shown value keeps the deadline it was shown with, which ends this too.
  const copyVisible = async (block, button, node) => {
    if (node.hasAttribute('data-empty')) return;   // it says "Empty": nothing to copy
    const op = begin(block, false);
    op.value = node.textContent;
    try {
      const done = await toClipboard(op, null);
      if (!live(op)) return;
      if (done) copied(block, button);
      else say(block, HOLD);
    } finally {
      end(op);
    }
  };

  // Copy on a hidden value: fetch and copy in one go; where that cannot copy, show the value
  // with the line on pressing and holding. The answer and the clipboard both count against the
  // operation's own deadline; once it has passed, a late answer of either shows nothing.
  const wireCopy = (button) => {
    const form = button.form;
    const block = form.closest('[data-value-block]');
    button.addEventListener('click', async (event) => {
      event.preventDefault();
      const shown = $('.v-revealed', block);
      if (shown) {
        await copyVisible(block, button, shown);
        return;
      }
      if (busy.has(form)) return;
      if (!codeOf(form).value) {
        say(block, FAILED);
        return;
      }
      say(block, '');
      const op = begin(block, true);
      const asked = ask(form, op);
      asked.catch(() => {});   // answered below
      try {
        const done = await toClipboard(op, asked);
        if (!live(op)) return;
        try {
          await asked;
        } catch (err) {
          if (live(op)) say(block, FAILED);
          return;
        }
        if (!live(op)) return;
        if (done) {
          copied(block, button);
        } else if (show(block, op.value) && op.value !== '') {
          say(block, HOLD);
        }
      } finally {
        end(op);
      }
    });
  };

  // the page that came back from a Show without this script links to the item to hide it:
  // that link becomes the same Show/Hide button as everywhere else
  for (const link of $$('.v-reveal a')) {
    const button = document.createElement('button');
    button.type = 'submit';
    button.name = 'then';
    button.value = 'show';
    button.className = 'v-button v-button--quiet';
    button.dataset.action = 'show';
    button.textContent = 'Hide';
    link.replaceWith(button);
  }
  for (const button of $$('.v-reveal [data-action="show"]')) wireShow(button);
  for (const button of $$('.v-reveal [data-action="copy"]')) wireCopy(button);
  // a value shown by the page itself goes away like one shown here, at once when the page
  // starts out of sight (an answer loaded in a background tab)
  if (document.visibilityState === 'hidden') hideAll();
  else for (const shown of $$('.v-revealed')) display(shown.closest('[data-value-block]'));

  // Copy on a plain value: it is on the page already
  for (const button of $$('[data-action="copy-plain"]')) {
    button.hidden = false;
    button.addEventListener('click', () => {
      const block = button.closest('[data-value-block]');
      copyVisible(block, button, $('[data-plain]', block));
    });
  }

  // -- the list's search: filters the rows by name as the person types
  const search = $('#search');
  const list = $('.v-list');
  if (search && list) {
    const rows = $$('.v-list-item', list);
    const none = $('#no-results');
    const count = $('#search-count');
    const filter = () => {
      const wanted = search.value.trim().toLowerCase();
      let left = 0;
      for (const row of rows) {
        row.hidden = !row.dataset.find.includes(wanted);
        if (!row.hidden) left += 1;
      }
      list.hidden = left === 0;
      none.hidden = left > 0;
      count.textContent = wanted ? (left === 1 ? words.countOne : words.countMany).replace('{}', String(left)) : '';
    };
    search.closest('.v-search').hidden = false;
    search.addEventListener('input', filter);
    $('[data-clear-search]').addEventListener('click', () => {
      search.value = '';
      filter();
      search.focus();
    });
    filter();   // a word the browser kept from an earlier visit
  }

  // -- the forms. A new-value slot is a real checkbox the page sends (add<N>); closing one keeps
  // what was typed in this page but turns its boxes off, so nothing of it is sent.
  for (const slot of $$('.v-slot')) {
    const open = $('.v-slot-enable', slot);
    const follow = (focus) => {
      for (const box of $$('input[type="text"], textarea', slot)) box.disabled = !open.checked;
      if (!focus) return;
      if (open.checked) $('textarea', slot).focus({preventScroll: true});
      else open.focus({preventScroll: true});
    };
    open.addEventListener('change', () => follow(true));
    follow(false);
  }
  // one-tap labels: they fill the label box and leave the keyboard down
  for (const group of $$('.v-picks')) {
    const input = $('input[type="text"]', group.closest('[data-new-value]'));
    const picks = $$('[data-label]', group);
    const mark = () => {
      for (const pick of picks) pick.setAttribute('aria-pressed', String(pick.dataset.label === input.value));
    };
    for (const pick of picks) {
      pick.addEventListener('click', () => {
        input.value = pick.dataset.label;
        mark();
      });
    }
    input.addEventListener('input', mark);
    mark();
    group.hidden = false;
  }
  // a stored value: Remove turns off its box (Undo turns it on again), and a hidden value's
  // new-value box is on only while Replace is ticked; nothing typed is thrown away. The value of
  // a box the item must keep has no Remove.
  const removed = (card) => Boolean($('.v-remove-input', card) && $('.v-remove-input', card).checked);
  for (const card of $$('.v-existing')) {
    const remove = $('.v-remove-input', card);
    const replace = $('.v-replace-input', card);
    const box = $('textarea', card);
    const follow = (focus) => {
      box.disabled = removed(card) || Boolean(replace && !replace.checked);
      if (replace) replace.disabled = removed(card);
      if (focus && !box.disabled) box.focus({preventScroll: true});
    };
    if (remove) remove.addEventListener('change', () => follow(false));
    if (replace) replace.addEventListener('change', () => follow(true));
    follow(false);
  }

  // Scroll a focused box to the middle once a phone's keyboard is up. Best effort: the room
  // under every form and the Save in the top bar work without it.
  let focusTimer;
  document.addEventListener('focusin', (event) => {
    const target = event.target;
    if (!target.matches || !target.matches('input[type="text"], input[type="search"], textarea')) return;
    clearTimeout(focusTimer);
    focusTimer = setTimeout(() => {
      if (document.activeElement === target) target.scrollIntoView({block: 'center'});
    }, 350);
  });

  // The slips the page would refuse anyway, said before the values are sent: a refused form
  // comes back without them. The page checks everything again.
  const refuse = (form, text, target) => {
    const box = $('#form-error');
    box.textContent = '';
    const title = document.createElement('p');
    const strong = document.createElement('strong');
    strong.textContent = words.notSaved;
    title.append(strong);
    const line = document.createElement('p');
    line.textContent = text;
    box.append(title, line);
    box.hidden = false;
    if (!target) {
      box.focus();
      return;
    }
    const details = target.closest('details');
    if (details) details.open = true;
    target.setAttribute('aria-invalid', 'true');
    target.focus();
    target.addEventListener('input', () => target.removeAttribute('aria-invalid'), {once: true});
  };
  const check = (form) => {
    const name = $('input[name="name"]', form);
    if (name && !name.value.trim()) return ['Enter a name.', name];
    const labels = new Set();
    let left = 0;
    for (const card of $$('.v-existing', form)) {
      if (removed(card)) continue;
      left += 1;
      labels.add($('h2', card).textContent);
      const box = $('.v-replace-input', card) && $('textarea', card);
      if (box && !box.disabled && box.value === '') return ['Enter a value or turn off Replace.', box];
    }
    for (const card of $$('[data-new-value]', form)) {
      const open = $('.v-slot-enable', card);
      if (open && !open.checked) continue;
      left += 1;
      const label = $('input[type="text"]', card);
      const value = $('textarea', card);
      if (!label.value.trim()) return ['Enter a label.', label];
      if (labels.has(label.value.trim())) return ['Each value needs a different label.', label];
      labels.add(label.value.trim());
      if (value.value === '') {
        return [open ? 'Enter a value, or remove this value card.' : 'Enter a value.', value];
      }
    }
    // a form of fixed boxes: a box left empty adds nothing, unless the item needs it
    for (const box of $$('[data-fixed-value]', form)) {
      if (box.value !== '') left += 1;
      else if (box.hasAttribute('data-required')) return [box.dataset.required, box];
    }
    if (!left && name) return ['Enter a value.', $('[data-fixed-value]', form)];
    if (!left) return [words.noValueLeft, null];
    return null;
  };
  // the add or edit form: the page's one form that saves
  const form = $('form.v-form');
  if (form && $('#form-error')) {
    form.addEventListener('submit', (event) => {
      const slip = check(form);
      if (!slip) return;
      event.preventDefault();
      refuse(form, ...slip);
    });
  }
})();
