/* Behaviour for the shared components in templates/components/macros.html.
   Loaded from base.html so every page that uses tabs() or a copy button gets
   it; do not re-implement these handlers per page. */
(function () {
  'use strict';

  /* Copy buttons: [data-ds-copy] copies the nearest [data-ds-copy-source]
     inside the same [data-ds-copy-root], so two snippets on one page never
     collide on ids. The result is announced through the root's live region. */
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-ds-copy]');
    if (!btn) return;
    var root = btn.closest('[data-ds-copy-root]');
    var source = root && root.querySelector('[data-ds-copy-source]');
    if (!source) return;
    var value = (source.innerText || source.textContent || '').trim();
    if (!value) return;
    var status = root.querySelector('[data-ds-copy-status]');
    var label = btn.getAttribute('data-ds-copy-label') || 'Copy';
    var done = btn.getAttribute('data-ds-copied-label') || 'Copied';
    /* Show the outcome on the button for sighted users and in the live
       region for screen readers, then restore the label. */
    function show(text, announcement) {
      btn.textContent = text;
      if (status) status.textContent = announcement;
      window.setTimeout(function () {
        btn.textContent = label;
        if (status) status.textContent = '';
      }, 1500);
    }
    function fail() {
      show('Not copied', 'Could not copy. Select the text and copy it by hand.');
    }
    if (!navigator.clipboard) { fail(); return; }
    navigator.clipboard.writeText(value).then(function () { show(done, done); }).catch(fail);
  });

  /* Tabs: WAI-ARIA tabs pattern with automatic activation. Click, Left/Right,
     Home and End move the selection; only the selected tab is in the tab
     order. Works for any number of tablists on a page. */
  function selectTab(list, tab, focus) {
    list.querySelectorAll('[role="tab"]').forEach(function (t) {
      var on = t === tab;
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      t.classList.toggle('is-active', on);
      t.tabIndex = on ? 0 : -1;
      var panel = document.getElementById(t.getAttribute('aria-controls'));
      if (panel) panel.hidden = !on;
    });
    if (focus) tab.focus();
  }

  document.addEventListener('click', function (e) {
    var tab = e.target.closest('.ds-tabs [role="tab"]');
    if (!tab) return;
    selectTab(tab.closest('[role="tablist"]'), tab, false);
  });

  document.addEventListener('keydown', function (e) {
    var tab = e.target.closest('.ds-tabs [role="tab"]');
    if (!tab) return;
    var list = tab.closest('[role="tablist"]');
    var tabs = Array.prototype.slice.call(list.querySelectorAll('[role="tab"]'));
    var i = tabs.indexOf(tab);
    var next = null;
    if (e.key === 'ArrowRight') next = tabs[(i + 1) % tabs.length];
    else if (e.key === 'ArrowLeft') next = tabs[(i - 1 + tabs.length) % tabs.length];
    else if (e.key === 'Home') next = tabs[0];
    else if (e.key === 'End') next = tabs[tabs.length - 1];
    if (!next) return;
    e.preventDefault();
    selectTab(list, next, true);
  });
})();
