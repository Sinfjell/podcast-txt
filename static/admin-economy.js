/* Admin economy page: period filter, cumulative view, KPI cards with
   click-to-breakdown, and the signups/transcripts/payers chart.
   Data comes from #econData (built by admin_economy.collect_economy). */
(function () {
    'use strict';
    var node = document.getElementById('econData');
    if (!node || typeof Chart === 'undefined') return;
    var D = JSON.parse(node.textContent);
    var css = getComputedStyle(document.documentElement);
    function token(n) { return css.getPropertyValue(n).trim(); }
    var C = {
        accent: token('--accent'), accentHover: token('--accent-hover'), ink: token('--ink'),
        muted: token('--muted'), line: token('--line'), lineStrong: token('--line-strong'),
        danger: token('--danger'), paper: token('--paper'),
    };
    var st = { preset: 'all', mode: 'cum', sel: 'revenue', from: D.days[0], to: D.days[D.days.length - 1] };
    var charts = { main: null, act: null, res: null };
    var S = D.series;

    function $(id) { return document.getElementById(id); }
    function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
    function money(v, d) {
        d = d == null ? 2 : d;
        var neg = v < 0 && Math.abs(v) >= Math.pow(10, -d) / 2;
        return (neg ? '−' : '') + '$' + Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
    }
    function num(v, d) { return v.toLocaleString('en-US', { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 }); }
    function pct(v) { return Math.round(v * 100) + ' %'; }
    function sum(a, i0, i1) { var s = 0; for (var i = i0; i <= i1; i++) s += a[i]; return s; }
    function cumul(a, i0, i1) { var o = [], s = 0; for (var i = i0; i <= i1; i++) { s += a[i]; o.push(s); } return o; }
    function slice(a, i0, i1) { return a.slice(i0, i1 + 1); }

    function range() {
        var n = D.days.length, i0 = 0, i1 = n - 1;
        if (st.preset === '7') i0 = Math.max(0, n - 7);
        else if (st.preset === '30') i0 = Math.max(0, n - 30);
        else if (st.preset === 'custom') {
            i0 = -1; i1 = -1;
            D.days.forEach(function (d, i) { if (i0 < 0 && d >= st.from) i0 = i; if (d <= st.to) i1 = i; });
            if (i0 < 0) i0 = n;
        }
        return [i0, i1];
    }
    function label(d) { var p = d.split('-'); return ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][+p[1] - 1] + ' ' + (+p[2]); }

    var KPI = [
        { k: 'revenue', name: 'Revenue', hint: 'after VAT and refunds' },
        { k: 'income', name: 'Income', hint: 'after Stripe fees' },
        { k: 'cost', name: 'Cost', hint: 'OpenAI and fixed costs' },
        { k: 'result', name: 'Result', hint: 'income minus cost' },
    ];

    function render() { var r = range(); renderKpis(r); renderResult(r); renderDetail(r); renderActivity(r); }

    function renderKpis(r) {
        var i0 = r[0], i1 = r[1], empty = i1 < i0, h = '';
        var cnt = empty ? 0 : sum(S.purchases, i0, i1);
        KPI.forEach(function (m) {
            var v = empty ? 0 : sum(S[m.k], i0, i1), sub = '';
            if (m.k === 'revenue') sub = cnt + ' purchase' + (cnt === 1 ? '' : 's') + (cnt ? ', ' + money(v / cnt) + ' each' : '');
            if (m.k === 'income') sub = empty ? '' : money(sum(S.fees, i0, i1)) + ' in Stripe fees';
            if (m.k === 'cost') { var tr = sum(S.cost_trial, i0, i1); sub = v > 0 ? pct(tr / v) + ' is free trial minutes' : ''; }
            if (m.k === 'result') sub = v < 0 ? 'We lose ' + money(-v) + ' in the period' : 'We make ' + money(v) + ' in the period';
            h += '<button type="button" class="econ-kpi" data-k="' + m.k + '" aria-pressed="' + (st.sel === m.k) + '">' +
                '<span class="econ-kpi__label">' + m.name + ' · ' + m.hint + '</span>' +
                '<span class="econ-kpi__value">' + money(v) + '</span>' +
                '<span class="econ-kpi__sub">' + esc(sub) + '</span></button>';
        });
        $('econKpis').innerHTML = h;
    }

    /* ---- result per day: bars per day, cumulative line on the right axis ---- */
    function renderResult(r) {
        var i0 = r[0], i1 = r[1];
        if (charts.res) { charts.res.destroy(); charts.res = null; }
        if (i1 < i0) { $('econResultLede').textContent = 'No days in this period.'; $('econResultLegend').innerHTML = ''; return; }
        var res = slice(S.result, i0, i1), total = sum(S.result, i0, i1), neg = res.filter(function (v) { return v < 0; }).length;
        $('econResultLede').textContent = 'Income minus cost, day by day. ' + (total < 0 ? 'Over the period we lose ' : 'Over the period we make ') + money(Math.abs(total)) +
            '; ' + neg + ' of ' + res.length + ' days are below zero.';
        $('econResultLegend').innerHTML = legendHtml([{ label: 'Result per day', color: C.accent }]) +
            '<span class="econ-legend__item"><span class="econ-legend__swatch econ-legend__swatch--danger"></span>Day below zero</span>' +
            '<span class="econ-legend__item"><span class="econ-legend__swatch"></span>Cumulative result (right axis)</span>';
        var o = chartOptions(true, false);
        o.scales.y2 = { position: 'right', grid: { drawOnChartArea: false }, ticks: { color: C.muted, font: { size: 12 }, callback: function (v) { return money(v, 0); } } };
        charts.res = new Chart($('econResultChart'), {
            type: 'bar',
            data: { labels: slice(D.days, i0, i1).map(label), datasets: [
                { type: 'bar', label: 'Result per day', data: res, yAxisID: 'y', borderRadius: 3,
                  backgroundColor: res.map(function (v) { return v < 0 ? C.danger : C.accent; }) },
                { type: 'line', label: 'Cumulative result', data: cumul(S.result, i0, i1), yAxisID: 'y2', borderColor: C.ink, backgroundColor: C.ink, borderWidth: 2, pointRadius: 0, tension: 0.2 },
            ] },
            options: o,
        });
    }

    function seriesFor(k) {
        if (k === 'revenue') return { items: [{ label: 'Revenue', color: C.accent, vals: S.revenue }] };
        if (k === 'income') return { items: [{ label: 'Income', color: C.accentHover, vals: S.income }] };
        if (k === 'cost') return {
            stack: true,
            items: [
                { label: 'Free trial minutes', color: C.ink, vals: S.cost_trial, sw: '' },
                { label: 'Paid minutes used', color: C.muted, vals: S.cost_paid, sw: 'muted' },
                { label: 'Fixed costs', color: C.lineStrong, vals: S.cost_fixed, sw: 'line' }],
        };
        return { items: [{ label: 'Result (red below zero)', color: C.ink, vals: S.result, signed: true }],
            context: [{ label: 'Income', color: C.accentHover, vals: S.income }, { label: 'Cost', color: C.muted, vals: S.cost }] };
    }
    var TITLES = {
        revenue: ['Revenue: how it breaks down', 'What customers paid, minus VAT and refunds. VAT goes to the state and is not our revenue.'],
        income: ['Income: what is left of the payment', 'Revenue minus Stripe fees. This is the money that lands with us.'],
        cost: ['Cost: where the money goes', 'OpenAI cost on the minutes we transcribe, plus fixed costs. Free trial minutes are usually the biggest item.'],
        result: ['Result: how much we lose or make', 'Income minus cost. Below zero means customers so far have cost more than they have paid.'],
    };

    function legendHtml(items) {
        return items.map(function (it) {
            var cls = it.sw === 'muted' ? ' econ-legend__swatch--muted' : it.sw === 'line' ? ' econ-legend__swatch--line' :
                it.color === C.accent ? ' econ-legend__swatch--accent' : it.color === C.accentHover ? ' econ-legend__swatch--hover' :
                it.color === C.muted ? ' econ-legend__swatch--muted' : '';
            return '<span class="econ-legend__item"><span class="econ-legend__swatch' + cls + '"></span>' + esc(it.label) + '</span>';
        }).join('');
    }

    function renderDetail(r) {
        var k = st.sel, i0 = r[0], i1 = r[1], empty = i1 < i0;
        $('econDetailTitle').textContent = TITLES[k][0];
        $('econDetailLede').textContent = TITLES[k][1];
        if (charts.main) { charts.main.destroy(); charts.main = null; }
        if (empty) { $('econLegend').innerHTML = ''; $('econBreakdown').innerHTML = '<p class="econ-note">No days in this period.</p>'; return; }
        var s = seriesFor(k), cum = st.mode === 'cum', labels = slice(D.days, i0, i1).map(label);
        var legendItems = s.items.slice();
        if (cum && s.context) legendItems = legendItems.concat(s.context);
        $('econLegend').innerHTML = legendHtml(legendItems);
        var datasets = [], type = 'bar';
        if (!cum) {
            s.items.forEach(function (it) {
                var vals = slice(it.vals, i0, i1);
                datasets.push({
                    type: 'bar', label: it.label, data: vals, stack: s.stack ? 's' : undefined,
                    backgroundColor: it.signed ? vals.map(function (v) { return v < 0 ? C.danger : C.accent; }) : it.color,
                    borderRadius: 3,
                });
            });
        } else {
            type = 'line';
            (s.context || []).forEach(function (it) {
                datasets.push({ label: it.label, data: cumul(it.vals, i0, i1), borderColor: it.color, borderDash: [5, 4], borderWidth: 1.5, pointRadius: 0, tension: 0.2 });
            });
            s.items.forEach(function (it) {
                var ds = { label: it.label, data: cumul(it.vals, i0, i1), borderColor: it.color, backgroundColor: it.color, borderWidth: 2.5, pointRadius: 3, tension: 0.2 };
                if (it.signed) ds.segment = { borderColor: function (ctx) { return ctx.p1.parsed.y < 0 ? C.danger : C.accent; } };
                datasets.push(ds);
            });
            if (s.stack) {
                datasets = s.items.map(function (it) { return { label: it.label, data: cumul(it.vals, i0, i1), borderColor: it.color, backgroundColor: it.color, borderWidth: 1.5, pointRadius: 0, tension: 0.2 }; });
                datasets.push({ label: 'Total cost', data: cumul(S.cost, i0, i1), borderColor: C.ink, backgroundColor: C.ink, borderWidth: 2.5, pointRadius: 3, tension: 0.2 });
            }
        }
        charts.main = new Chart($('econChart'), {
            type: type, data: { labels: labels, datasets: datasets },
            options: chartOptions(true, !!(s.stack && !cum)),
        });
        $('econBreakdown').innerHTML = breakdown(k, i0, i1);
        fillBars();
    }

    function chartOptions(isMoney, stacked) {
        return {
            responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { display: false },
                tooltip: { callbacks: { label: function (c) { return c.dataset.label + ': ' + (isMoney ? money(c.parsed.y) : num(c.parsed.y, c.parsed.y % 1 ? 1 : 0)); } } },
            },
            scales: {
                x: { stacked: stacked, ticks: { color: C.muted, maxRotation: 0, autoSkip: true, maxTicksLimit: 8, font: { size: 12 } }, grid: { color: C.line } },
                y: { stacked: stacked, beginAtZero: true, ticks: { color: C.muted, font: { size: 12 }, callback: function (v) { return isMoney ? money(v, 0) : num(v); } }, grid: { color: C.line } },
            },
        };
    }

    /* ---- breakdowns ---- */
    function periodPurchases(i0, i1) { var a = D.days[i0], b = D.days[i1]; return D.purchases.filter(function (p) { return p.d >= a && p.d <= b; }); }
    function group(list, keyf, valf) { var m = {}; list.forEach(function (p) { var k = keyf(p); m[k] = m[k] || { v: 0, n: 0 }; m[k].v += valf(p); m[k].n += 1; }); return m; }
    function rows(m, labelf) { return Object.keys(m).map(function (k) { return { k: labelf(k), v: m[k].v, n: m[k].n }; }).sort(function (a, b) { return b.v - a.v; }); }
    function bars(list, cls, fmt) {
        var mx = Math.max.apply(null, list.map(function (r) { return Math.abs(r.v); }).concat([0.0001]));
        return '<div class="econ-bars">' + list.map(function (r) {
            return '<div class="econ-bar"><span class="econ-bar__name">' + esc(r.k) + '</span><span class="econ-bar__track"><span class="econ-bar__fill ' + (r.cls || cls || '') + '" data-w="' + (Math.abs(r.v) / mx * 100).toFixed(1) + '"></span></span><span class="econ-bar__value">' + (fmt ? fmt(r) : money(r.v)) + '</span></div>';
        }).join('') + '</div>';
    }
    function fillBars() { document.querySelectorAll('#econBreakdown .econ-bar__fill').forEach(function (el) { el.style.width = el.getAttribute('data-w') + '%'; }); }
    function block(title, inner) { return '<div><h3 class="econ-h">' + title + '</h3>' + inner + '</div>'; }
    function stat(v, l) { return '<div class="econ-stat"><span class="econ-stat__value">' + v + '</span><span class="econ-stat__label">' + l + '</span></div>'; }

    function breakdown(k, i0, i1) {
        var list = periodPurchases(i0, i1), h = '';
        if (k === 'revenue' || k === 'income') {
            var f = k === 'revenue' ? function (p) { return p.revenue; } : function (p) { return p.revenue - p.fee; };
            var fmt = function (r) { return money(r.v) + ' <span>· ' + r.n + '</span>'; };
            h += block('By pack', bars(rows(group(list, function (p) { return p.plan; }, f), function (x) { return D.plan_labels[x] || x; }), '', fmt));
            h += block('By country', bars(rows(group(list, function (p) { return p.country || 'Unknown'; }, f), function (x) { return x; }), '', fmt));
            var nr = rows(group(list, function (p) { return p.new ? 'new' : 'repeat'; }, f), function (x) { return x === 'new' ? 'First purchase' : 'Repeat purchase'; });
            var rep = nr.filter(function (r) { return r.k === 'Repeat purchase'; })[0];
            h += block('First purchase and repeat', bars(nr, '', fmt) + '<p class="econ-note">' + (rep ? rep.n : 0) + ' of ' + list.length + ' purchases are repeats.</p>');
            if (k === 'revenue') h += block('By how the amount was chosen', bars(rows(group(list, function (p) { return p.src || 'fixed pack'; }, f), function (x) { return x; }), '', fmt));
            else {
                var g = sum(S.gross, i0, i1), v = sum(S.vat, i0, i1), rf = sum(S.refunds, i0, i1), fe = sum(S.fees, i0, i1);
                h += block('From payment to income', '<div class="econ-bars">' + [['Customers paid', g], ['VAT', -v], ['Refunds', -rf], ['Stripe fees', -fe], ['Income', g - v - rf - fe]].map(function (x) { return '<div class="econ-bar"><span class="econ-bar__name">' + x[0] + '</span><span></span><span class="econ-bar__value">' + money(x[1]) + '</span></div>'; }).join('') + '</div>');
            }
        } else if (k === 'cost') {
            var t = sum(S.cost_trial, i0, i1), p = sum(S.cost_paid, i0, i1), fx = sum(S.cost_fixed, i0, i1), tot = t + p + fx;
            var pc = function (r) { return money(r.v) + ' <span>· ' + pct(tot ? r.v / tot : 0) + '</span>'; };
            h += block('Split', bars([{ k: 'Free trial minutes', v: t, cls: 'econ-bar__fill--ink' }, { k: 'Paid minutes used', v: p, cls: 'econ-bar__fill--muted' }, { k: 'Fixed costs', v: fx }], '', pc) +
                '<p class="econ-note">' + num(sum(S.trial_min, i0, i1)) + ' trial minutes and ' + num(sum(S.paid_min, i0, i1)) + ' paid minutes transcribed.</p>');
            h += block('Cost still ahead', '<div class="econ-stats">' + stat(num(D.unused_paid_minutes) + ' min', 'Paid minutes not used yet') + stat(money(D.unused_paid_cost_usd), 'What they cost when used. Not in the cost above.') + '</div>');
        } else {
            var inc = sum(S.income, i0, i1), cost = sum(S.cost, i0, i1), cnt = list.length, tr = sum(S.cost_trial, i0, i1), res = inc - cost, days = i1 - i0 + 1;
            h += block('Per purchase, on average', '<div class="econ-stats">' + stat(cnt ? money(inc / cnt) : '–', 'Income per purchase') + stat(cnt ? money(tr / cnt) : '–', 'Trial cost per purchase (all trial users, not only buyers)') + stat(cnt ? money(res / cnt) : '–', 'Result per purchase, all costs') + '</div>');
            h += block('Pace', '<div class="econ-stats">' + stat(money(res / days), 'Result per day') + stat(money(res / days * 30), 'If the pace holds for 30 days') + '</div><p class="econ-note">Based on ' + days + ' days and ' + cnt + ' purchases. Read it as a pointer.</p>');
            h += block('Income against cost', bars([{ k: 'Income', v: inc }, { k: 'Cost', v: cost, cls: 'econ-bar__fill--ink' }, { k: 'Result', v: res, cls: res < 0 ? 'econ-bar__fill--danger' : '' }], '', function (r) { return money(r.v); }));
        }
        return h;
    }

    /* ---- activity chart ---- */
    function renderActivity(r) {
        var i0 = r[0], i1 = r[1];
        if (charts.act) { charts.act.destroy(); charts.act = null; }
        if (i1 < i0) { $('econActLegend').innerHTML = ''; return; }
        var m = $('econActMetric').value, aud = $('econActAudience').value, A = D.activity, cum = st.mode === 'cum';
        var all = A[m], paying = m === 'new_payers' ? A.new_payers : A[m + '_paying'];
        var isMin = m === 'minutes';
        function pick(a) { return cum ? cumul(a, i0, i1) : slice(a, i0, i1); }
        var labels = slice(D.days, i0, i1).map(label), datasets = [], items = [];
        if (m === 'new_payers') { aud = 'paying'; }
        if (aud === 'paying') {
            datasets.push({ label: 'Paying users', data: pick(paying), backgroundColor: C.accent, borderColor: C.accent, borderWidth: cum ? 2.5 : 0, pointRadius: cum ? 3 : 0, tension: 0.2, borderRadius: 3 });
            items.push({ label: 'Paying users', color: C.accent });
        } else if (aud === 'all') {
            datasets.push({ label: 'All users', data: pick(all), backgroundColor: C.ink, borderColor: C.ink, borderWidth: cum ? 2.5 : 0, pointRadius: cum ? 3 : 0, tension: 0.2, borderRadius: 3 });
            items.push({ label: 'All users', color: C.ink });
        } else {
            var rest = all.map(function (v, i) { return Math.max(0, v - paying[i]); });
            datasets.push({ label: 'Paying users', data: pick(paying), backgroundColor: C.accent, borderColor: C.accent, borderWidth: cum ? 2 : 0, pointRadius: 0, tension: 0.2, stack: 'a', borderRadius: 3, fill: cum });
            datasets.push({ label: 'Others', data: pick(rest), backgroundColor: C.muted, borderColor: C.muted, borderWidth: cum ? 2 : 0, pointRadius: 0, tension: 0.2, stack: 'a', borderRadius: 3, fill: cum });
            items.push({ label: 'Paying users', color: C.accent }, { label: 'Others', color: C.muted });
        }
        $('econActLegend').innerHTML = legendHtml(items);
        var stacked = aud === 'split';
        var opts = chartOptions(false, stacked);
        if (isMin) opts.plugins.tooltip.callbacks.label = function (c) { return c.dataset.label + ': ' + num(c.parsed.y) + ' min'; };
        charts.act = new Chart($('econActChart'), { type: cum ? 'line' : 'bar', data: { labels: labels, datasets: datasets }, options: opts });
    }

    /* ---- events ---- */
    function setPressed(group, attr, val) { group.querySelectorAll('[' + attr + ']').forEach(function (b) { b.setAttribute('aria-pressed', b.getAttribute(attr) === val ? 'true' : 'false'); }); }
    $('econKpis').addEventListener('click', function (e) { var b = e.target.closest('.econ-kpi'); if (!b) return; st.sel = b.getAttribute('data-k'); var r = range(); renderKpis(r); renderDetail(r); });
    $('econPeriod').addEventListener('click', function (e) { var b = e.target.closest('[data-preset]'); if (!b) return; st.preset = b.getAttribute('data-preset'); setPressed($('econPeriod'), 'data-preset', st.preset); $('econDates').hidden = st.preset !== 'custom'; render(); });
    $('econMode').addEventListener('click', function (e) { var b = e.target.closest('[data-mode]'); if (!b) return; st.mode = b.getAttribute('data-mode'); setPressed($('econMode'), 'data-mode', st.mode); render(); });
    ['econFrom', 'econTo'].forEach(function (id) { $(id).addEventListener('change', function () { st.from = $('econFrom').value || D.days[0]; st.to = $('econTo').value || D.days[D.days.length - 1]; render(); }); });
    ['econActMetric', 'econActAudience'].forEach(function (id) { $(id).addEventListener('change', function () { renderActivity(range()); }); });
    render();
})();
