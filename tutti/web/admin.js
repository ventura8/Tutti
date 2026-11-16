// Tutti dashboard: the Admin tab (#admin; docs/ADMIN.md). Costs per provider and account from GET /api/admin (admins only):
// this month so far, the forecast, the monthly fee, usage against the limit, the month history, and every admin control
// with a link to where it lives. A row Tutti cannot price says "not configured" and which setting fills it. Uses app.js's el, $.
'use strict';

// Pure (tests run it in node): one money figure, or a dash.
function money(v, cur) {
    return v === null || v === undefined ? '—' : `${Number(v).toFixed(2)} ${cur}`;
}

// Pure: the cost rows as label lines.
function adminCostRows(data) {
    const cur = data.currency || '';
    return (data.rows || []).map((r) => {
        const u = r.usage && r.usage.used_pct !== null && r.usage.used_pct !== undefined ? ` · used ${r.usage.used_pct}% (${r.usage.verdict})` : (r.usage ? ` · ${r.usage.verdict}` : '');
        const what = r.configured
            ? `month to date ${money(r.mtd, cur)} · forecast ${money(r.forecast, cur)}${r.monthly !== null && r.monthly !== undefined ? ` · fee ${money(r.monthly, cur)}/month` : ''}`
            : `not configured: ${r.how}`;
        const est = r.kind === 'electricity' && r.configured ? (r.measured ? ' (measured)' : ' (estimated)') : '';
        return { name: `${r.name} · ${r.kind}`, text: what + est + u, configured: !!r.configured };
    });
}

// Pure: the headline.
function adminMeta(data) {
    const t = data.totals || {};
    const cur = data.currency || '';
    return `${data.month}, day ${data.day} of ${data.days}: ${money(t.mtd, cur)} so far, forecast ${money(t.forecast, cur)}`
        + (t.unknown ? ` · ${t.unknown} not configured` : '');
}

async function loadAdmin() {
    const r = await fetch('/api/admin', { cache: 'no-store' }).catch(() => null);
    if (!r || !r.ok) {
        $('costs-meta').textContent = r && r.status === 403 ? 'Only an admin sees costs: ask an admin for the role.' : 'Costs could not be loaded: the dashboard did not answer. Refresh to try again.';
        return;
    }
    const data = await r.json();
    $('costs-meta').textContent = adminMeta(data);
    $('costs').replaceChildren(...adminCostRows(data).map((x) => el('li', { class: `row${x.configured ? '' : ' row--muted'}` },
        el('strong', { text: x.name }), el('span', { class: 'row__note', text: ` ${x.text}` }))));
    const hist = (data.history || []).map((h) => el('li', { class: 'row', text: `${h.month}: ${money(h.total, h.currency)} (forecast then ${money(h.forecast, h.currency)})` }));
    $('costs-history').replaceChildren(...(hist.length ? hist : [el('li', { class: 'row', text: 'No month recorded yet.' })]));
    $('admin-controls').replaceChildren(...(data.controls || []).map((c) => el('li', { class: 'row' },
        el('a', { href: c.href, text: c.name }), el('span', { class: 'row__note', text: ` ${c.state}` }))));
}

function showAdminView() {
    const on = location.hash.startsWith('#admin');
    $('admin-view').hidden = !on;
    if (!on) return;
    for (const id of ['dashboard-view', 'settings-view', 'chat-view', 'users-view']) { const n = $(id); if (n) n.hidden = true; }
    for (const a of document.querySelectorAll('.tabs__tab[data-view]')) {
        if (a.dataset.view === 'admin') a.setAttribute('aria-current', 'page');
        else a.removeAttribute('aria-current');
    }
    void loadAdmin();
}

if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('DOMContentLoaded', async () => {
        const me = await fetch('/api/auth/me', { cache: 'no-store' }).then((r) => r.json()).catch(() => null);
        if (!me || (me.enabled && me.role !== 'admin')) return;   // sign-in off: this computer's owner is the admin
        $('admin-tab').hidden = false;
        window.addEventListener('hashchange', () => setTimeout(showAdminView, 0));   // after the other views settle
        showAdminView();
    });
}

if (typeof module !== 'undefined') module.exports = { money, adminCostRows, adminMeta };
