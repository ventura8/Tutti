// Tutti dashboard: the household part of the admin-only Users tab (users.js draws people, roles, invites and the sign-in's
// audit; docs/AUTH.md). Drawn into #users-manage: every household member, the admin's own row marked "(you)", with main
// machine, presence, last seen, whether their Tutti joined, mode, schedule and consumption, every action an admin can take
// for them (their own Tutti carries it out), and the household audit, filterable by user (docs/HOUSEHOLD.md, *The Users
// tab*). Uses app.js's el, $.
'use strict';

const HH_USER_ACTS = [
    ['schedule', 'Schedule and away'], ['main-agent', 'Main agent'], ['main-machine', 'Main machine'], ['connect', 'Reconnect agents'],
    ['pair-device', 'Pair a device'], ['machine-use', 'Use a shared machine'], ['pause', 'Pause a machine'], ['resume', 'Resume a machine'],
    ['update', 'Update Tutti and agents'], ['notify', 'Questions outside work hours'], ['join', 'Join or rejoin, install'], ['make-admin', 'Make admin'], ['remove', 'Remove'],
];
const HH_MODES = ['auto', 'active', 'away'];
const HH_UT = { data: null, filter: '' };

// Pure (tests run it in node): the rows of the Users tab from POST /api/users/manage {list: true}.
function hhUserRows(list, now = Date.now()) {
    const ago = (t) => (t ? `${Math.max(0, Math.round((now / 1000 - t) / 60))} min ago` : 'never');
    return (list.members || []).map((m) => {
        const s = m.setup || {};
        const stats = (s.stats || []).map((x) => `${x.kind}: ${x.used === undefined || x.used === null ? '?' : x.used}${x.verdict ? ' ' + x.verdict : ''}`);
        return {
            hash: m.hash, name: m.name + (m.me ? ' (you)' : ''), me: !!m.me, mainMachine: s.main_machine || 'unknown',
            agents: s.agents || [], targets: s.targets || [], mode: s.mode || 'auto', schedule: (s.schedule || []).join(' · '),
            stats: stats.join(' · ') || 'no readings yet', seen: ago(m.last_seen), joined: m.household_of_one ? 'this Tutti (a household of one)' : (m.joined ? 'Tutti joined' : 'Tutti not joined'),
            presence: m.active ? 'prompting' : (m.away ? 'away' : (m.online ? 'online' : 'offline')),
            // every row gets every control; the last-admin guard only disables (with the reason) what would leave no admin
            acts: HH_USER_ACTS.map(([a, label]) => {
                const guarded = !!((a === 'remove' || a === 'make-admin') && m.admin && (list.admins || 1) <= 1);
                return { act: a, label, disabled: guarded, why: guarded ? `${m.name} is the only admin: make another member admin first` : '' };
            }),
        };
    });
}

// Pure: the audit lines, newest first, of one user or everyone.
function hhAuditRows(list, who) {
    return (list.audit || []).filter((e) => !who || e.by === who || e.for === who).map((e) => e.text);
}

async function hhUsersPost(body) {
    const res = await fetch('/api/users/manage', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN }, body: JSON.stringify(body),
    });
    const answer = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
    return answer;
}

function hhArgsFor(act, row, form) {
    const v = (name) => form.querySelector(`[name="${name}"]`)?.value;
    if (act === 'schedule') {
        const out = v('mode') === 'vacation' ? { mode: `vacation-until-${v('until')}` } : { mode: v('mode') };
        const days = [...form.querySelectorAll('[name="day"]:checked')].map((x) => x.value);
        if (days.length) out.days = days;
        if (v('window')) out.window = v('window');
        if (v('hours')) out.hours = Number(v('hours'));
        return out;
    }
    if (act === 'main-agent') return { kind: v('kind') };
    if (act === 'notify') return { notify: v('notify'), batch: Number(v('batch') || 3) };
    if (act === 'main-machine') return { machine: v('machine') };
    if (['machine-use', 'pause', 'resume'].includes(act)) return { machine: v('machine') };
    return {};
}

function hhActForm(row) {
    const form = el('form', { class: 'user__acts', onsubmit: (e) => e.preventDefault() },
        el('label', {}, 'Mode ', el('select', { name: 'mode' }, ...[...HH_MODES, 'vacation'].map((m) => el('option', { value: m, text: m, selected: m === row.mode })))),
        el('label', {}, 'until ', el('input', { name: 'until', type: 'date' })),
        el('fieldset', { class: 'seg' }, el('legend', { text: 'Working days' }),
            ...['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'].map((d) => el('label', {}, el('input', { type: 'checkbox', name: 'day', value: d }), d))),
        el('label', {}, 'Hours window ', el('input', { name: 'window', placeholder: '09-17', size: 5 })),
        el('label', {}, 'Hours a day ', el('input', { name: 'hours', type: 'number', min: 1, max: 24 })),
        el('label', {}, 'Questions ', el('select', { name: 'notify' }, el('option', { value: 'work-hours', text: 'in work hours' }),
            el('option', { value: 'always', text: 'outside work hours too' }))),
        el('label', {}, 'after ', el('input', { name: 'batch', type: 'number', min: 1, max: 50, value: 3, size: 3 }), ' wait'),
        el('label', {}, 'Agent ', el('select', { name: 'kind' }, ...row.agents.map((a) => el('option', { value: a, text: a })))),
        el('label', {}, 'Machine ', el('select', { name: 'machine' }, ...row.targets.map((t) => el('option', { value: t, text: t })))));
    const note = el('p', { class: 'row__note', 'aria-live': 'polite' });
    for (const a of row.acts) {
        form.append(el('button', {
            class: 'btn btn--tonal', type: 'button', text: a.label, 'data-act': a.act, disabled: a.disabled, title: a.why || undefined,
            onclick: async () => {
                if (a.act === 'join') { note.textContent = `Hand ${row.name} the invite from Invites below: one command or one scan installs Tutti and joins.`; return; }
                if ((a.act === 'remove' || a.act === 'make-admin') && !window.confirm(`${a.label}: ${row.name}?`)) return;
                try {
                    const r = await hhUsersPost({ member: row.hash, act: a.act, args: hhArgsFor(a.act, row, form) });
                    note.textContent = r.hand_over ? r.hand_over.replace('{name}', row.name) : `${a.label}: asked; ${row.name}'s Tutti does it within a minute.`;
                    void hhLoadUsers();
                } catch (err) { note.textContent = err.message; }
            },
        }));
    }
    form.append(note);
    return form;
}

function hhFrame() {
    const host = $('users-manage');
    if (!host || $('users-tab-list')) return;
    host.append(
        el('section', { class: 'card span-full', 'aria-labelledby': 'users-tab-h' },
            el('header', { class: 'card__head' }, el('h2', { id: 'users-tab-h', text: 'Household' }), el('p', { class: 'card__meta', id: 'users-tab-meta' })),
            el('ul', { id: 'users-tab-list', class: 'list', 'aria-label': 'Household members' })),
        el('section', { class: 'card span-full', 'aria-labelledby': 'users-audit-h' },
            el('header', { class: 'card__head' }, el('h2', { id: 'users-audit-h', text: 'Household audit' }),
                el('label', { for: 'users-audit-filter', text: 'Show ' }),
                el('select', { id: 'users-audit-filter', class: 'pick', onchange: (e) => { HH_UT.filter = e.target.value; hhRenderUsersTab(); } },
                    el('option', { value: '', text: 'Everyone' }))),
            el('ul', { id: 'users-audit', class: 'list', 'aria-label': 'Household audit' })));
}

function hhRenderUsersTab() {
    hhFrame();
    const list = HH_UT.data || {};
    $('users-tab-meta').textContent = list.error || `${(list.members || []).length} user(s) in the household`;
    $('users-tab-list').replaceChildren(...hhUserRows(list).map((r) => el('li', { class: 'row user' },
        el('div', { class: 'row__name' }, r.name, el('span', { class: 'row__kind', text: `main machine ${r.mainMachine}` })),
        el('p', { class: 'row__note', text: `${r.joined} · ${r.presence} · last seen ${r.seen} · mode ${r.mode} · ${r.schedule}` }),
        el('p', { class: 'row__note', text: r.stats }),
        el('details', {}, el('summary', { text: `Manage ${r.name}` }), hhActForm(r)))));
    const who = $('users-audit-filter');
    const names = (list.members || []).map((m) => m.name);
    if (who.options.length !== names.length + 1) who.replaceChildren(el('option', { value: '', text: 'Everyone' }), ...names.map((n) => el('option', { value: n, text: n })));
    $('users-audit').replaceChildren(...hhAuditRows(list, HH_UT.filter).map((t) => el('li', { class: 'row__note', text: t })));
}

async function hhLoadUsers() {
    try { HH_UT.data = await hhUsersPost({ list: true }); } catch (err) { HH_UT.data = { error: err.message }; }
    hhRenderUsersTab();
}

if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('DOMContentLoaded', () => {
        const go = () => { if ((location.hash || '').startsWith('#users') && document.body.dataset.role !== 'viewer') void hhLoadUsers(); };
        window.addEventListener('hashchange', go);
        go();
    });
}
