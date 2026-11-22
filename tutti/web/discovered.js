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
        ...d.can.map((c) => el('p', { class: 'row__note', text: `Could be: ${c}` })),
        d.setup ? setupBox(d) : null);
}

// t225: the one command that sets it up, to run in a terminal on this computer; the password (a computer's) is typed there
function setupBox(d) {
    const copy = el('button', { class: 'btn btn--text', type: 'button', text: 'Copy', 'aria-label': `Copy the set-up command for ${d.address}`,
        onclick: async (e) => {
            const btn = e.currentTarget;
            try { await navigator.clipboard.writeText(d.setup); btn.textContent = 'Copied'; } catch { btn.textContent = 'Select and copy it'; }
        } });
    const how = d.kind === 'android' ? 'It prints the steps (allow debugging on the device, then the config lines to add): the device is set up once you follow them.'
        : 'It makes a key if needed, then ssh asks for that computer\'s password once: type it there. Tutti never sees it.';
    return el('details', { class: 'qitem', 'data-setup': d.address }, el('summary', { text: 'Set up' }),
        el('p', { class: 'row__note', text: 'Run this in a terminal on this computer:' }),
        el('p', {}, el('code', { text: d.setup }), ' ', copy),
        el('p', { class: 'row__note', text: how }));
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

// The Machines tab's Home Assistant card (t268): GET /api/home-assistant when the tab opens
function haNodes(d) {
    if (!d.configured) return [el('p', { class: 'row__note', text: 'Not connected. Add home_assistant: {url: http://homeassistant.local:8123} to the config, and a long-lived access token in ~/.config/tutti/home-assistant.env (docs/MACHINES.md).' })];
    if (!d.item) return [el('p', { class: 'row__note', role: 'alert', text: d.problem })];
    const h = d.item;
    return [el('p', { class: 'row__name', text: `${h.name} · ${h.address}` }),
        el('p', { class: 'row__note', text: [h.online ? 'online' : 'offline', h.version && `version ${h.version}`, h.time_zone].filter(Boolean).join(' · ') }),
        el('p', { class: 'row__note', text: `Roles: ${h.roles.map((r) => r.role).join(', ')}` }),
        h.problem ? el('p', { class: 'row__note', role: 'status', text: h.problem }) : null].filter(Boolean);
}

async function loadHa() {
    const box = document.getElementById('ha-card');
    if (!box) return;
    try {
        const res = await fetch('/api/home-assistant', { cache: 'no-store' });
        const d = await res.json();
        if (!res.ok) throw new Error(d.error || `the dashboard answered ${res.status}`);
        box.replaceChildren(...haNodes(d));
    } catch (err) {
        box.replaceChildren(el('p', { class: 'row__note', role: 'alert', text: `Cannot ask Home Assistant: ${err.message}` }));
    }
}

if (typeof document === 'object' && document.addEventListener) {
    const haSync = () => { if (typeof tuttiView === 'function' && tuttiView(location.hash) === 'machines') void loadHa(); };
    document.addEventListener('DOMContentLoaded', haSync);
    window.addEventListener('hashchange', haSync);
}

if (typeof module !== 'undefined') module.exports.haNodes = haNodes;

// The Devices tab's list (t208): every configured device with its state, then the media devices found on the network
function deviceListNodes(s) {
    const items = s.devices?.items || [];
    const lan = s.devices?.lan || [];
    const rows = items.map((d) => el('li', { class: 'row', 'data-device': d.name },
        el('p', { class: 'row__name', text: `${d.name} · ${d.kind}` }),
        el('p', { class: 'row__note', text: [d.status, d.reason, d.battery != null ? `battery ${d.battery} %` : ''].filter(Boolean).join(' · ') })));
    rows.push(...lan.map((d) => el('li', { class: 'row', 'data-device': d.address },
        el('p', { class: 'row__name', text: `${d.name} · on the network` }),
        el('p', { class: 'row__note', text: (d.roles || []).map((r) => r.role).join(', ') || 'no role yet' }))));
    return rows.length ? rows : [el('li', { class: 'row__note', text: 'No device yet: pair one (below).' })];
}

function renderDeviceList(s) {
    const list = document.getElementById('device-list');
    if (list) list.replaceChildren(...deviceListNodes(s));
}

if (typeof module !== 'undefined') Object.assign(module.exports, { deviceListNodes });

// The Machines tab's role matrix (t234): per machine, every role it has, could have (turned off) or cannot, why, and the fix
const MATRIX_STATE = { has: 'Has', off: 'Could have (turned off)', cannot: 'Cannot' };

function matrixRow(r) {
    return el('li', { class: 'row', 'data-state': r.state },
        el('p', { class: 'row__name', text: `${r.role} · ${MATRIX_STATE[r.state]}` }),
        el('p', { class: 'row__note', text: r.why }),
        r.fix ? el('p', { class: 'row__note' }, 'Fix: ', el('code', { text: r.fix })) : null);
}

function matrixNodes(d) {
    if (!d.machines.length) return [el('p', { class: 'row__note', text: 'No machine in the config yet: the Discovered tab finds them.' })];
    return d.machines.map((m) => el('details', { class: 'qitem', 'data-matrix': m.name },
        el('summary', { text: `${m.name}: ${m.has} roles, ${m.off} turned off, ${m.cannot} it cannot have` }),
        el('ul', { class: 'list' }, ...m.rows.map(matrixRow))));
}

async function loadMatrix() {
    const box = document.getElementById('role-matrix');
    if (!box) return;
    const open = new Set([...box.querySelectorAll('details[open]')].map((x) => x.dataset.matrix));
    try {
        const res = await fetch('/api/role-matrix', { cache: 'no-store' });
        const d = await res.json();
        if (!res.ok) throw new Error(d.error || `the dashboard answered ${res.status}`);
        box.replaceChildren(...matrixNodes(d));
        for (const n of box.querySelectorAll('details[data-matrix]')) n.open = open.has(n.dataset.matrix);
    } catch (err) {
        box.replaceChildren(el('p', { class: 'row__note', role: 'alert', text: `Cannot show the roles: ${err.message}` }));
    }
}

if (typeof document === 'object' && document.addEventListener) {
    const matrixSync = () => { if (typeof tuttiView === 'function' && tuttiView(location.hash) === 'machines') void loadMatrix(); };
    document.addEventListener('DOMContentLoaded', matrixSync);
    window.addEventListener('hashchange', matrixSync);
}

if (typeof module !== 'undefined') Object.assign(module.exports, { matrixNodes });
