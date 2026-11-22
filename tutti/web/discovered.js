// Tutti dashboard: the Discovered tab (#discovered, t220, docs/DASHBOARD.md *Discovered*). What is on the network and not
// set up yet, from GET /api/discovered (admin-only); checked again every minute while the tab is open. Uses app.js's el().
'use strict';

const DISCOVERED = { timer: 0, loading: false };
const KIND_LABEL = { computer: 'Computer (ssh answers)', android: 'Android device (adb)', media: 'Media device', host: 'Something on the network' };

function discoveredRow(d) {
    return el('li', { class: 'row', 'data-discovered': d.address },
        el('div', { class: 'row__name', text: d.name === d.address ? d.address : `${d.name} · ${d.address}` }),
        el('p', { class: 'row__note', text: `${KIND_LABEL[d.kind] || d.kind} · found by ${d.found_by.join(', ')}` }),
        el('p', { class: 'row__note', text: d.roles.length ? `Roles now: ${d.roles.join(', ')}` : 'No role yet' }),
        ...d.can.map((c) => el('p', { class: 'row__note', text: `Could be: ${c}` })));
}

function drawDiscovered(d) {
    const list = document.getElementById('discovered-list');
    if (!list) return;
    list.replaceChildren(...(d.items.length ? d.items.map(discoveredRow)
        : [el('li', { class: 'row__note', text: 'Nothing new on the network: everything found is set up already.' })]));
    const meta = document.getElementById('discovered-meta');
    if (meta) meta.textContent = `${d.items.length} found${d.problems.length ? ` · ${d.problems.join('; ')}` : ''}`;
}

async function loadDiscovered() {
    if (DISCOVERED.loading) return;
    DISCOVERED.loading = true;
    try {
        const res = await fetch('/api/discovered', { cache: 'no-store' });
        const d = await res.json();
        if (!res.ok) throw new Error(d.error || `the dashboard answered ${res.status}`);
        drawDiscovered(d);
    } catch (err) {
        document.getElementById('discovered-meta')?.replaceChildren(`Cannot look: ${err.message}`);
    } finally {
        DISCOVERED.loading = false;
    }
}

if (typeof document === 'object' && document.addEventListener) {
    const sync = () => {
        const on = typeof tuttiView === 'function' && tuttiView(location.hash) === 'discovered' && document.body.dataset.role !== 'viewer';
        clearInterval(DISCOVERED.timer);
        if (!on) return;
        void loadDiscovered();
        DISCOVERED.timer = setInterval(() => { void loadDiscovered(); }, 60000);
    };
    document.addEventListener('DOMContentLoaded', () => {
        document.getElementById('discovered-refresh')?.addEventListener('click', () => { void loadDiscovered(); });
        sync();
    });
    window.addEventListener('hashchange', sync);
}

if (typeof module !== 'undefined') module.exports = { discoveredRow, drawDiscovered };
