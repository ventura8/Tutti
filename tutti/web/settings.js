// Tutti dashboard: the Settings tab. Every config key from GET /api/settings, grouped like tutti.config.yaml, each with its
// control, its value, its default and "Reset to default"; a change is one POST /api/settings, saved into the config file
// (comments kept) after the server's own check. Secrets are never sent to this page: only "set" or "not set" and the
// command that sets them. DOM nodes and textContent only (no HTML from data). Loads on its own: node tests run it too.
'use strict';

const SET = { data: null, query: '', changed: false, msgs: new Map(), loading: null, wanted: '' };
const DAY_NAMES = [['mon', 'Mon'], ['tue', 'Tue'], ['wed', 'Wed'], ['thu', 'Thu'], ['fri', 'Fri'], ['sat', 'Sat'], ['sun', 'Sun']];

const PROPS = { text: (n, v) => { n.textContent = v; }, class: (n, v) => { n.className = v; }, value: (n, v) => { n.value = v; },
    checked: (n, v) => { n.checked = Boolean(v); } };

function setProp(node, k, v) {
    if (PROPS[k]) PROPS[k](node, v);
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : String(v));
}

function h(tag, props, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) if (v !== undefined && v !== null && v !== false) setProp(node, k, v);
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) node.append(kid);
    return node;
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const domId = (key) => `set-${key.replace(/[^A-Za-z0-9_-]/g, '_')}`;

function show(v, row) {
    if (v === null || v === undefined || v === '') return '(not set)';
    if (typeof v === 'boolean') return v ? 'on' : 'off';
    if (Array.isArray(v)) return v.length ? v.join(', ') : '(none)';
    if (typeof v === 'object') return JSON.stringify(v);
    return row?.unit ? `${v} ${row.unit}` : String(v);
}

// The value in effect: a switch kept in the state folder wins over the file until it is saved here.
const effective = (row) => (row.override ? row.override.value : row.value);

function isChanged(row) {
    if (row.control === 'secret') return false;
    if (row.runtime) return Boolean(row.value);
    if (row.override) return !('default' in row) || !same(row.override.value, row.default);
    if (!row.in_file) return false;
    return !('default' in row) || !same(row.value, row.default);
}

function matches(row, query, entry) {
    if (!query) return true;
    const text = `${row.label} ${row.help} ${row.key} ${entry || ''}`.toLowerCase();
    return query.toLowerCase().split(/\s+/).filter(Boolean).every((w) => text.includes(w));
}

const visible = (row, entry) => matches(row, SET.query, entry) && (!SET.changed || isChanged(row));

// ---- saving

function headers() {
    const token = document.querySelector('meta[name="tutti-token"]')?.content || '';
    return { 'Content-Type': 'application/json', 'X-Tutti-Token': token };
}

async function saveSetting(row, body) {
    SET.msgs.set(row.key, { text: 'Saving…', tone: 'idle' });
    drawSettings();
    try {
        const res = await fetch('/api/settings', { method: 'POST', headers: headers(), body: JSON.stringify({ path: row.path, etag: SET.data?.etag, ...body }) });
        const answer = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
        const restart = (answer.restart || []).length > 0;
        SET.msgs.set(row.key, { text: restart ? 'Saved in the file: takes effect on restart.' : 'Saved in the file: applies now.', tone: 'ok', restart });
        await loadSettings(true);
        if (typeof refresh === 'function') void refresh();
    } catch (err) {
        SET.msgs.set(row.key, { text: `Not saved: ${err.message}`, tone: 'bad' });
        if (/changed since this page loaded/.test(err.message)) await loadSettings(true);
        else drawSettings();
    }
}

const started = () => fetch('/api/health', { cache: 'no-store' }).then((r) => r.json()).then((j) => j.started).catch(() => null);

// Reloads only once a new process answers (its start time changed), so a restart that failed is never taken for done.
async function restartDashboard(button) {
    button.disabled = true;
    button.textContent = 'Restarting…';
    const before = await started();
    const res = await fetch('/api/restart', { method: 'POST', headers: headers(), body: '{}' }).catch(() => null);
    if (!res?.ok) {
        button.textContent = 'Not restarted: the dashboard refused it. Restart tutti dashboard yourself.';
        return;
    }
    for (let i = 0; i < 40; i += 1) {
        await new Promise((r) => { setTimeout(r, 500); });
        const now = await started();
        if (now && now !== before) { location.reload(); return; }
    }
    button.textContent = 'Not back after 20 s: start it again with tutti dashboard';
}

// ---- controls

function listEditor(row, id) {
    const items = [...(effective(row) || [])];
    const listId = `${id}-choices`;
    const input = h('input', { id, type: 'text', class: 'setting__input', list: row.choices ? listId : null,
        placeholder: row.control === 'timespans' ? 'HH:MM-HH:MM' : `Add ${row.item || 'a name'}`,
        pattern: row.control === 'timespans' ? String.raw`\d{2}:\d{2}-\d{2}:\d{2}` : null });
    const add = () => {
        const v = input.value.trim();
        if (!v || items.includes(v)) return;
        if (row.control === 'timespans' && !/^\d{2}:\d{2}-\d{2}:\d{2}$/.test(v)) {
            SET.msgs.set(row.key, { text: 'Write a span as HH:MM-HH:MM, for example 22:00-06:00.', tone: 'bad' });
            drawSettings();
            return;
        }
        void saveSetting(row, { value: [...items, v] });
    };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } });
    return h('div', { class: 'setting__list' },
        h('ul', { class: 'setting__items', 'aria-label': `${row.label}: in order` }, ...items.map((v, i) => h('li', { class: 'setting__item' },
            h('span', { text: v }),
            h('button', { class: 'btn btn--text', type: 'button', 'aria-label': `Remove ${v}`,
                onclick: () => saveSetting(row, { value: items.filter((_, j) => j !== i) }) }, '×')))),
        h('span', { class: 'setting__add' }, input, h('button', { class: 'btn btn--tonal', type: 'button', onclick: add }, 'Add')),
        row.choices ? h('datalist', { id: listId }, ...row.choices.filter((c) => !items.includes(c)).map((c) => h('option', { value: c }))) : null);
}

// An ordered list (project priority): most important first; each move is saved at once.
function orderEditor(row, id) {
    const items = [...(row.value || [])];
    const move = (i, d) => {
        const next = [...items];
        [next[i], next[i + d]] = [next[i + d], next[i]];
        void saveSetting(row, { order: next });
    };
    return h('ol', { class: 'setting__order', id, 'aria-label': `${row.label}: most important first` }, ...items.map((v, i) => h('li', { class: 'setting__item' },
        h('span', { text: `${i}. ${v}` }),
        h('button', { class: 'btn btn--text', type: 'button', 'aria-label': `Move ${v} up`, disabled: i === 0, onclick: () => move(i, -1) }, '↑'),
        h('button', { class: 'btn btn--text', type: 'button', 'aria-label': `Move ${v} down`, disabled: i === items.length - 1, onclick: () => move(i, 1) }, '↓'))));
}

function daysPicker(row, id) {
    const on = new Set(effective(row) || []);
    return h('fieldset', { class: 'setting__days', id }, h('legend', { class: 'sr-only', text: row.label }),
        ...DAY_NAMES.map(([d, name]) => h('label', { class: 'setting__day' },
            h('input', { type: 'checkbox', checked: on.has(d), onchange: (e) => {
                const next = DAY_NAMES.map(([x]) => x).filter((x) => (x === d ? e.target.checked : on.has(x)));
                void saveSetting(row, { value: next });
            } }), h('span', { text: name }))));
}

function hoursPicker(row, id) {
    const [from, to] = String(effective(row) || '00-00').split('-').map(Number);
    const pick = (label, v, idOf) => h('label', { class: 'setting__hour' }, h('span', { text: label }),
        h('input', { id: idOf, type: 'number', min: 0, max: 24, step: 1, inputmode: 'numeric', value: String(v), class: 'setting__input setting__input--short',
            onchange: () => {
                const a = String(document.getElementById(id).value).padStart(2, '0');
                const b = String(document.getElementById(`${id}-to`).value).padStart(2, '0');
                void saveSetting(row, { value: `${a}-${b}` });
            } }));
    return h('span', { class: 'setting__hours' }, pick('from', from, id), pick('to', to, `${id}-to`));
}

function selectOf(row, id) {
    const value = effective(row);
    const options = [...(row.choices || [])];
    if (value !== null && value !== undefined && !options.includes(value)) options.unshift(value);
    const blank = !('default' in row) || row.default === null || row.default === '';
    return h('select', { id, class: 'pick', onchange: (e) => saveSetting(row, e.target.value === '' ? { reset: true } : { value: e.target.value }) },
        blank ? h('option', { value: '', text: '(not set)', selected: value === null || value === undefined || value === '' }) : null,
        ...options.map((c) => h('option', { value: c, text: String(c), selected: c === value })));
}

function yamlEditor(row, id) {
    const area = h('textarea', { id, class: 'setting__yaml', rows: Math.min(12, Math.max(2, (row.yaml || '').split('\n').length)),
        spellcheck: 'false', readonly: row.readonly, value: row.yaml || '' });
    return h('div', { class: 'setting__yamlbox' }, area, row.readonly
        ? h('p', { class: 'setting__note', text: 'Holds a secret, so it is shown with the secret hidden and is changed in the file only.' })
        : h('button', { class: 'btn btn--tonal', type: 'button', onclick: () => saveSetting(row, { yaml: area.value }) }, 'Save'));
}

function secretBox(row) {
    const how = h('p', { class: 'setting__note', hidden: true,
        text: `Run ${row.setup} in a terminal on this computer: it asks for the value without showing it and keeps it out of the config file.` });
    return h('span', { class: 'setting__secret' }, h('span', { class: 'chip', 'data-tone': row.set ? 'ok' : 'idle', text: row.set ? 'set' : 'not set' }),
        h('button', { class: 'btn btn--text', type: 'button', onclick: () => { how.hidden = !how.hidden; } }, 'How to set it'), how);
}

function plainInput(row, id) {
    const number = row.control === 'number';
    const value = effective(row);
    const listId = row.suggest ? `${id}-suggest` : null;
    const input = h(row.control === 'textarea' ? 'textarea' : 'input', { id, class: 'setting__input', type: number ? 'number' : 'text',
        min: row.min, max: row.max, step: row.step || (number ? 'any' : null), inputmode: number ? 'decimal' : null, list: listId,
        value: value === null || value === undefined ? '' : String(value), rows: row.control === 'textarea' ? 4 : null,
        onchange: (e) => {
            const raw = e.target.value.trim();
            if (raw === '') return void saveSetting(row, 'default' in row && row.default !== null ? { reset: true } : { value: null });
            return void saveSetting(row, { value: number ? Number(raw) : raw });
        } });
    return h('span', { class: 'setting__field' }, input, row.unit ? h('span', { class: 'setting__unit', text: row.unit }) : null,
        listId ? h('datalist', { id: listId }, ...row.suggest.map((s) => h('option', { value: s }))) : null);
}

function controlOf(row, id) {
    switch (row.control) {
    case 'switch':
        return h('input', { id, type: 'checkbox', role: 'switch', class: 'setting__switch', checked: Boolean(effective(row)),
            onchange: (e) => saveSetting(row, { value: e.target.checked }) });
    case 'select': return selectOf(row, id);
    case 'list': case 'timespans': return listEditor(row, id);
    case 'days': return daysPicker(row, id);
    case 'order': return orderEditor(row, id);
    case 'hours': return hoursPicker(row, id);
    case 'yaml': return yamlEditor(row, id);
    case 'secret': return secretBox(row);
    default: return plainInput(row, id);
    }
}

// ---- rows, entries and sections

function metaLine(row) {
    const parts = [];
    if ('default' in row && row.control !== 'secret') parts.push(`Default: ${show(row.default, row)}`);
    if (row.override) parts.push(`In effect: ${show(row.override.value, row)}, set by ${row.override.by}; the file says ${show(row.value, row)}`);
    if (row.note) parts.push(row.note);
    return parts.join(' · ');
}

function resetButton(row) {
    const known = 'default' in row;
    const title = known ? `Back to ${show(row.default, row)}` : 'Take the key out of the file';
    return h('button', { class: 'btn btn--text', type: 'button', title, onclick: () => saveSetting(row, { reset: true }) },
        known ? 'Reset to default' : 'Remove from the file');
}

function settingRow(row) {
    const id = domId(row.key);
    const msg = SET.msgs.get(row.key);
    const resettable = (row.in_file || row.override) && !['secret', 'order'].includes(row.control) && !row.runtime;
    return h('div', { class: 'setting', id: `${id}-row`, 'data-key': row.key },
        h('div', { class: 'setting__text' },
            h('label', { class: 'setting__label', for: row.control === 'secret' ? null : id, text: row.label }),
            h('code', { class: 'setting__key', text: row.key }),
            isChanged(row) ? h('span', { class: 'chip', 'data-tone': 'accent', text: 'changed' }) : null,
            row.restart ? h('span', { class: 'chip', 'data-tone': 'warn', text: 'on restart' }) : null,
            row.help ? h('p', { class: 'setting__help', text: row.help }) : null,
            metaLine(row) ? h('p', { class: 'setting__meta', text: metaLine(row) }) : null),
        h('div', { class: 'setting__control' }, controlOf(row, id),
            resettable ? resetButton(row) : null),
        msg ? message(msg) : null);
}

function message(msg) {
    const restart = msg.restart ? h('button', { class: 'btn btn--tonal', type: 'button', onclick: (e) => restartDashboard(e.currentTarget) }, 'Restart') : null;
    return h('p', { class: 'setting__msg', 'data-tone': msg.tone, role: 'status' }, msg.text, restart);
}

function entryBlock(group, e) {
    const rows = e.rows.filter((r) => visible(r, e.name));
    if (!rows.length) return null;
    const changed = e.rows.filter(isChanged).length;
    const state = changed ? `${changed} changed` : 'as the defaults say';
    return h('details', { class: 'entry', id: `${domId(e.key)}-row`, open: Boolean(SET.query || SET.changed || SET.wanted.startsWith(e.key)) },
        h('summary', { class: 'entry__head' }, h('span', { class: 'entry__name', text: e.name }),
            h('span', { class: 'entry__kind', text: `${group.title} · ${state}` })),
        ...rows.map((r) => settingRow(r)));
}

function sectionBlock(s) {
    const rows = s.rows.filter((r) => visible(r));
    const groups = s.groups.map((g) => {
        const entries = g.entries.map((e) => entryBlock(g, e)).filter(Boolean);
        if (!entries.length) return (SET.query || SET.changed) ? null : h('p', { class: 'setting__note', text: `No ${g.title} in the file yet.` });
        return h('div', { class: 'entries' }, h('h3', { class: 'card__sub', text: g.title }), ...entries);
    }).filter(Boolean);
    if (!rows.length && !groups.length) return null;
    return h('section', { class: 'card setting-card', id: `settings-${s.id}`, 'aria-labelledby': `settings-${s.id}-h`, 'data-scope': s.scope },
        h('header', { class: 'card__head' }, h('h3', { class: 'setting-card__title', id: `settings-${s.id}-h`, text: s.title })),
        h('p', { class: 'setting__help', text: s.help }), ...rows.map((r) => settingRow(r)), ...groups);
}

function secretsBlock(list) {
    return h('section', { class: 'card setting-card', id: 'settings-secrets', 'aria-labelledby': 'settings-secrets-h' },
        h('header', { class: 'card__head' }, h('h3', { class: 'setting-card__title', id: 'settings-secrets-h', text: 'Secrets' })),
        h('p', { class: 'setting__help', text: 'Never shown or changed here: each is kept outside the config file. Its setup command sets it.' }),
        h('ul', { class: 'list' }, ...(list || []).map((s) => h('li', { class: 'setting' },
            h('div', { class: 'setting__text' }, h('span', { class: 'setting__label', text: s.label }), h('code', { class: 'setting__key', text: s.where })),
            h('div', { class: 'setting__control' }, secretBox({ set: s.set, setup: s.setup }))))));
}

// Another copy of the config that drifted apart (docs/CONFIG.md): the warning and its one-click merge.
function copiesBlock(copies) {
    if (!copies?.length) return '';
    const button = h('button', { class: 'btn btn--filled', type: 'button', text: 'Merge into one' });
    const msg = h('p', { class: 'setting__msg', role: 'status' });
    button.addEventListener('click', async () => {
        button.disabled = true;
        const res = await fetch('/api/settings', { method: 'POST', headers: headers(), body: JSON.stringify({ action: 'merge-configs' }) }).catch(() => null);
        const answer = res ? await res.json().catch(() => ({})) : { error: 'the dashboard did not answer: is it running?' };
        if (res?.ok) { await loadSettings(true); return; }
        button.disabled = false;
        msg.dataset.tone = 'bad';
        msg.textContent = `Not merged: ${answer.error || `the dashboard answered ${res?.status}`}`;
    });
    return h('section', { class: 'card setting-card', id: 'settings-copies', role: 'alert' },
        h('p', { class: 'setting__help', text: `${copies.length} other copy(ies) of the config differ from the one in use; Tutti reads only the one in use, so what only a copy holds is not used:` }),
        h('ul', { class: 'list' }, ...copies.map((c) => h('li', { class: 'setting__note',
            text: `${c.path}: ${c.missing.length} setting(s) only there, ${c.conflicts.length} with other values${c.conflicts.length ? ` (${c.conflicts.join(', ')})` : ''}` }))),
        h('p', { class: 'setting__help', text: 'Merge into one adds what only a copy has (the file in use wins where both differ), keeps each copy as a backup and links it to the one file.' }),
        button, msg);
}

function renderSettings(data) {
    if (!data) return [h('p', { class: 'setting__note', text: 'Loading the settings…' })];
    const sections = data.sections.map(sectionBlock).filter(Boolean);
    
    // Create sidebar navigation
    const navItems = data.sections.filter((s) => s.rows.length || s.groups.some((g) => g.entries.length))
        .map((s) => h('a', { class: 'settings-nav-item', href: `#settings-${s.id}`, text: s.title }));
    const nav = h('nav', { class: 'settings-sidebar', 'aria-label': 'Settings navigation' }, h('div', { class: 'settings-nav' }, ...navItems));
    
    const head = h('p', { class: 'setting__note' }, data.writable ? `Saved into ${data.path}, its comments and order kept; a backup is made first.`
        : `${data.path || 'The config'} cannot be saved from here (only a YAML file you may write can): the values are shown read-only.`);
    
    const content = h('div', { class: 'settings-content' }, 
        head, 
        copiesBlock(data.copies), 
        ...(SET.query || SET.changed ? [] : [secretsBlock(data.secrets)]),
        ...(sections.length ? sections : [h('p', { class: 'setting__note', text: 'No setting matches: clear the search or the filter.' })])
    );
    
    return [h('div', { class: 'settings-layout' }, nav, content)];
}

let scrollSpyObserver = null;

function initScrollSpy() {
    if (scrollSpyObserver) scrollSpyObserver.disconnect();
    scrollSpyObserver = new IntersectionObserver((entries) => {
        let maxRatio = 0;
        let activeId = null;
        entries.forEach(entry => {
            if (entry.isIntersecting && entry.intersectionRatio > maxRatio) {
                maxRatio = entry.intersectionRatio;
                activeId = entry.target.id;
            }
        });
        if (activeId) {
            document.querySelectorAll('.settings-nav-item').forEach(nav => {
                nav.classList.toggle('settings-nav-item--active', nav.getAttribute('href') === `#${activeId}`);
            });
        }
    }, { rootMargin: '-100px 0px -60% 0px', threshold: [0, 0.25, 0.5, 0.75, 1] });

    document.querySelectorAll('.setting-card').forEach(section => scrollSpyObserver.observe(section));
}

// ---- the tab

function drawSettings() {
    const box = document.getElementById('settings-body');
    if (!box) return;
    const active = document.activeElement?.id;
    box.replaceChildren(...renderSettings(SET.data));
    if (active && active !== 'settings-search' && document.getElementById(active)) document.getElementById(active).focus({ preventScroll: true });
    
    // Initialize scroll spy
    initScrollSpy();
}

async function loadSettings(force) {
    if (SET.loading && !force) return SET.loading;
    SET.loading = (async () => {
        try {
            const res = await fetch('/api/settings', { cache: 'no-store' });
            if (!res.ok) throw new Error(`the dashboard answered ${res.status}`);
            SET.data = await res.json();
        } catch (err) {
            const box = document.getElementById('settings-body');
            if (box) box.replaceChildren(h('p', { class: 'setting__msg', 'data-tone': 'bad', role: 'alert',
                text: `The settings did not load (${err.message}): is tutti dashboard running? Reload to try again.` }));
            return;
        } finally {
            SET.loading = null;
        }
        drawSettings();
    })();
    return SET.loading;
}

function goTo(target) {
    const node = document.getElementById(`settings-${target}`) || document.getElementById(`${domId(target)}-row`);
    if (node) node.scrollIntoView({ block: 'start' });
}

async function showView() {
    const hash = location.hash || '';
    // the views by hash: #settings, #users (users.js), #dashboard; anything else is the Chat (chat.js), the default
    const view = hash.startsWith('#settings') ? 'settings' : hash.startsWith('#dashboard') ? 'dashboard' : hash.startsWith('#users') && document.body.dataset.role === 'admin' ? 'users' : 'chat';
    const settings = view === 'settings';
    document.getElementById('dashboard-view').hidden = view !== 'dashboard';
    document.getElementById('settings-view').hidden = !settings;
    for (const a of document.querySelectorAll('[data-view]')) {
        if (a.dataset.view === view) a.setAttribute('aria-current', 'page');
        else a.removeAttribute('aria-current');
    }
    if (!settings) return;
    SET.wanted = decodeURIComponent(hash.slice('#settings/'.length));
    if (!SET.data) await loadSettings();
    else drawSettings();
    if (SET.wanted) goTo(SET.wanted);
}

if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('DOMContentLoaded', () => {
        window.addEventListener('hashchange', () => { void showView(); });
        document.getElementById('settings-search')?.addEventListener('input', (e) => { SET.query = e.target.value.trim(); drawSettings(); });
        document.getElementById('settings-changed')?.addEventListener('change', (e) => { SET.changed = e.target.checked; drawSettings(); });
        void showView();
    });
}

if (typeof module !== 'undefined') module.exports = { renderSettings, isChanged, matches, show, SET };
