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
  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* Sticky header: the hairline appears once the page has scrolled. */
  var header = document.querySelector('[data-ds-header]');
  if (header) {
    var onScroll = function () { header.toggleAttribute('data-scrolled', window.scrollY > 8); };
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
  }

  /* Account menu in the signed-in header. On phones the toggle is hidden and
     the menu's links sit in the slide-down panel, so this only runs on desktop. */
  document.querySelectorAll('[data-nav-account]').forEach(function (root) {
    var toggle = root.querySelector('.nav-account__toggle');
    if (!toggle) return;
    function setOpen(open) {
      root.toggleAttribute('data-open', open);
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    toggle.addEventListener('click', function () { setOpen(!root.hasAttribute('data-open')); });
    document.addEventListener('click', function (e) { if (!root.contains(e.target)) setOpen(false); });
    root.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && root.hasAttribute('data-open')) { setOpen(false); toggle.focus(); }
    });
    root.addEventListener('focusout', function (e) {
      if (e.relatedTarget && !root.contains(e.relatedTarget)) setOpen(false);
    });
  });

  /* Typed placeholder: [data-ds-type-placeholder] holds a JSON list of
     examples. It types them in turn until the visitor touches the field (or
     anything in the same command bar), then leaves the real placeholder. */
  document.querySelectorAll('[data-ds-type-placeholder]').forEach(function (input) {
    if (reduceMotion) return;
    var examples;
    try { examples = JSON.parse(input.getAttribute('data-ds-type-placeholder')); } catch (e) { return; }
    if (!examples || !examples.length) return;
    var original = input.placeholder, k = 0, c = 0, deleting = false, stopped = false, timer;
    function stop() {
      if (stopped) return;
      stopped = true; window.clearTimeout(timer);
      if (input.placeholder.indexOf('​') === 0) input.placeholder = original;
    }
    var scope = input.closest('.ds-cmd') || input;
    ['focusin', 'pointerdown'].forEach(function (ev) { scope.addEventListener(ev, stop); });
    function tick() {
      if (stopped) return;
      var word = examples[k];
      c += deleting ? -1 : 1;
      /* The zero-width prefix marks a typed placeholder, so stop() can tell
         it from one the page's own script has set since. */
      input.placeholder = '​' + word.slice(0, Math.max(c, 0));
      if (!deleting && c >= word.length) { deleting = true; timer = window.setTimeout(tick, 1800); return; }
      if (deleting && c <= 0) { deleting = false; k = (k + 1) % examples.length; }
      timer = window.setTimeout(tick, deleting ? 22 : 55);
    }
    timer = window.setTimeout(tick, 900);
  });

  /* Signal wave: the hero illustration. Sound bars sweep past a scan line and
     come out as lines of text (the logo's idea at hero scale). Drawn on a
     canvas behind [data-ds-signal-root]; the band sits just above the
     element marked [data-ds-signal-anchor]. Paused off screen and in a
     background tab; a single still frame under reduced motion. */
  document.querySelectorAll('canvas[data-ds-signal]').forEach(function (cv) {
    var cx = cv.getContext('2d');
    if (!cx) return;
    var root = cv.closest('[data-ds-signal-root]') || cv.parentElement;
    var anchor = root.querySelector('[data-ds-signal-anchor]');
    var W = 0, H = 0, col = {}, visible = true, raf = 0;
    function colors() {
      var s = getComputedStyle(document.documentElement);
      col = { idle: s.getPropertyValue('--line').trim(), accent: s.getPropertyValue('--accent').trim(),
              glow: s.getPropertyValue('--glow').trim() };
    }
    function size() {
      var dpr = Math.min(window.devicePixelRatio || 1, 2);
      W = cv.clientWidth; H = cv.clientHeight;
      cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
      cx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    function amp(i) {
      var a = Math.sin(i * 0.37) * 0.5 + Math.sin(i * 0.11 + 1) * 0.35 + Math.sin(i * 1.7) * 0.15;
      return 0.18 + Math.abs(a) * 0.82;
    }
    function bar(x, y, w, h) {
      cx.beginPath();
      if (cx.roundRect) cx.roundRect(x, y, w, h, Math.min(w, h) / 2); else cx.rect(x, y, w, h);
      cx.fill();
    }
    function draw(t) {
      raf = 0;
      cx.clearRect(0, 0, W, H);
      var narrow = W < 600, step = narrow ? 9 : 12, maxH = narrow ? 90 : 150;
      var mid = anchor ? anchor.offsetTop - maxH / 2 - 12 : H / 2;
      var p = reduceMotion ? 0.5 : (t % 9000) / 9000;
      var scan = -W * 0.1 + p * W * 1.2;
      for (var i = 0, n = Math.ceil(W / step) + 1; i < n; i++) {
        var x = i * step;
        if (x < scan - 40) {
          var row = i % 3;
          cx.globalAlpha = Math.max(0, 0.55 - (scan - x) / (W * 1.4));
          cx.fillStyle = col.accent;
          bar(x, mid - 18 + row * 18, step * (row === 2 ? 0.7 : 1.6), 3);
        } else {
          var wob = reduceMotion ? 1 : 0.75 + 0.25 * Math.sin(t / 420 + i * 0.6);
          var h = amp(i) * maxH * wob, near = Math.max(0, 1 - Math.abs(x - scan) / 80);
          cx.globalAlpha = near > 0 ? 0.25 + near * 0.6 : 1;
          cx.fillStyle = near > 0 ? col.accent : col.idle;
          bar(x, mid - h / 2, 3, h);
        }
      }
      cx.globalAlpha = 1;
      var g = cx.createRadialGradient(scan, mid, 0, scan, mid, 260);
      g.addColorStop(0, col.glow); g.addColorStop(1, 'transparent');
      cx.fillStyle = g; cx.fillRect(scan - 260, mid - 260, 520, 520);
      if (!reduceMotion && visible && !document.hidden) raf = window.requestAnimationFrame(draw);
    }
    function start() { if (!raf) raf = window.requestAnimationFrame(draw); }
    colors(); size();
    window.addEventListener('resize', function () { size(); start(); });
    if (window.matchMedia) window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () { colors(); start(); });
    document.addEventListener('visibilitychange', start);
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (entries) { visible = entries[0].isIntersecting; if (visible) start(); }).observe(cv);
    }
    start();
  });

  /* Transcript window: the playhead walks through the timestamped lines. */
  document.querySelectorAll('[data-ds-window]').forEach(function (win) {
    var lines = win.querySelectorAll('.ds-window__line');
    var head = win.querySelector('.ds-window__playhead');
    if (!lines.length) return;
    var cur = 0;
    function step() {
      lines.forEach(function (l, i) { l.classList.toggle('is-current', i === cur); });
      if (head) head.style.width = Math.round(((cur + 1) / (lines.length + 1)) * 100) + '%';
      cur = (cur + 1) % lines.length;
    }
    step();
    if (!reduceMotion) window.setInterval(function () { if (!document.hidden) step(); }, 2600);
  });
})();
