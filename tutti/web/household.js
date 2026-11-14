// Tutti dashboard: the household (docs/HOUSEHOLD.md). "Me" shows this user's own setup; "Household" adds the household
// card: who is in it, who is active, each person's main agent, the shared machines' use per person and their fairness.
// Others are named, never detailed: their jobs show only as "busy for <name>". Uses app.js's helpers (el, $, chip, relative).
'use strict';

const HOUSEHOLD_VIEW = 'tutti-view';   // localStorage: me or household, per browser
const AGENT_NAMES = { claude: 'Claude Code', codex: 'Codex', agy: 'Antigravity', antigravity: 'Antigravity', cursor: 'Cursor',
    copilot: 'Copilot', gemini: 'Gemini', ollama: 'local model', local: 'local model' };

// Pure (tests run it in node): the household card's rows from /api/state's `household`.
function householdRows(h, now = Date.now()) {
    if (!h || h.role === 'alone') return { title: 'Household of one', note: 'Invite someone from the Users card: they run one command and their Tutti joins.', members: [], machines: [] };
    const presence = (m) => {
        if (m.active) return 'prompting now';
        if (m.online) return 'online';
        return m.last_seen ? `last seen ${Math.round((now / 1000 - m.last_seen) / 60)} min ago` : 'not seen yet';
    };
    const toneOf = (m) => {
        if (m.active) return 'accent';
        return m.online ? 'ok' : 'idle';
    };
    const members = (h.members || []).map((m) => ({ name: m.name + (m.me ? ' (you)' : ''), agent: AGENT_NAMES[m.agent] || m.agent || 'unknown agent',
        state: presence(m), tone: toneOf(m), runs: `${m.runs_today || 0} run(s) today` }));
    const machines = Object.entries(h.machines || {}).sort((a, b) => a[0].localeCompare(b[0])).map(([name, m]) => {
        const use = Object.entries(m.use || {}).map(([who, n]) => `${who === h.me ? 'you' : who} ${n}`).join(', ') || 'idle';
        const fair = m.fairness === null || m.fairness === undefined ? '' : ` · fairness ${Math.round(m.fairness * 100)} %`;
        return { name, owner: m.owner, note: `${m.owner}'s · ${m.slots} slot(s) · ${use} · your share now ${m.grant}${fair}`, paused: !!m.paused };
    });
    const why = h.error ? ' (' + h.error + ')' : '';
    let where = `coordinator not reachable${why}: working alone, shared projects wait`;
    if (h.reachable) where = `coordinator answered ${Math.round((now / 1000 - (h.at || 0)))} s ago`;
    if (h.away) where = `${h.away_note} (your accounts and machines as usual; shared tasks wait and sync when you are back)`;
    return { title: `${h.household} · ${(h.members || []).length} member(s)`, note: where, members, machines, reachable: !!h.reachable };
}

// Pure: what a machine row says about the household's use of it (others by name only), or ''.
function busyFor(m) {
    const u = m.household_use;
    if (!u) return '';
    const others = Object.entries(u.others || {}).map(([who, n]) => `${who} (${n})`);
    if (u.paused) return `household: paused by ${u.owner}`;
    const who = others.length ? 'busy for ' + others.join(', ') : 'nobody else on it';
    return `household: ${who} · your share ${u.grant}`;
}

function householdView() {
    try { return localStorage.getItem(HOUSEHOLD_VIEW) === 'household' ? 'household' : 'me'; } catch { return 'me'; }
}

function setHouseholdView(v) {
    try { localStorage.setItem(HOUSEHOLD_VIEW, v); } catch { /* private window: only this page load */ }
    document.body.dataset.view = v;
    for (const b of document.querySelectorAll('[data-view-pick]')) b.setAttribute('aria-pressed', String(b.dataset.viewPick === v));
}

function renderHousehold(s) {
    const r = householdRows(s.household);
    const away = $('household-away');   // shown in both views: the one thing a member away from home needs to know
    if (away) {
        away.hidden = !s.household?.away;
        away.textContent = s.household?.away_note || '';
    }
    $('household-meta').textContent = r.note;
    $('household-title').textContent = r.title;
    $('household-members').replaceChildren(...r.members.map((m) => el('li', { class: 'row' },
        el('div', { class: 'row__name' }, m.name, el('span', { class: 'row__kind', text: m.agent })),
        chip(m.state, m.tone), el('div', { class: 'row__stats' }, el('span', { text: m.runs })))));
    $('household-machines').replaceChildren(...(r.machines.length ? r.machines.map((m) => el('li', { class: 'row' },
        el('div', { class: 'row__name' }, m.name), m.paused ? chip('paused', 'warn') : chip('shared', 'ok'),
        el('p', { class: 'row__note', text: m.note })))
        : [el('li', { class: 'row__note', text: 'No shared machines: `tutti household machine share <m>` lends one.' })]));
}

if (typeof document !== 'undefined') {
    document.addEventListener('DOMContentLoaded', () => {
        for (const b of document.querySelectorAll('[data-view-pick]')) b.addEventListener('click', () => setHouseholdView(b.dataset.viewPick));
        setHouseholdView(householdView());
    });
}
