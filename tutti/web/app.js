// Tutti dashboard: polls /api/state and renders it with DOM nodes and textContent only (no HTML from data).
'use strict';

const $ = (id) => document.getElementById(id);
const FILTERS = [['all', 'All'], ['failed', 'Failed'], ['done', 'Done']];   // finished jobs; running and stalled ones have their own card
const LIVE = new Set(['running', 'stalled']);
const VERDICT_TONE = { 'on pace': 'ok', ahead: 'warn', behind: 'warn', late: 'bad', spent: 'bad', 'no reading': 'idle', unknown: 'idle' };
const MACHINE_TONE = { free: 'ok', loaded: 'warn', busy: 'accent', paused: 'warn', offline: 'bad', held: 'warn', off: 'idle' };
const JOB_TONE = { running: 'accent', stalled: 'warn', failed: 'bad', done: 'ok', ended: 'idle' };
const ui = { filter: 'all', project: '', open: new Set(), last: null, fetchedAt: 0, timer: 0, failing: false, seq: 0, editing: new Map(), accounts: new Map(),
    expanded: new Set(), collapsed: new Set() };
// What the owner opened or closed (a job, a details fold, Show more, the filters) is kept by item id across refreshes and reloads
// of this tab: tuttiKept (attention.js) reads and writes it to sessionStorage.
const UI_KEY = 'tutti-ui';
const TOKEN = document.querySelector('meta[name="tutti-token"]')?.content || '';
const PAGE_VERSION = document.querySelector('meta[name="tutti-version"]')?.content || '';
const RELOADED_FOR = 'tutti-reloaded-for';   // sessionStorage: the version this tab already reloaded for (never a loop)
const RESET_FROM = { reading: 'from the latest reading', estimate: 'an estimate from the config: add a reading to set it', config: 'from the config' };

function saveUi() {
    try { sessionStorage.setItem(UI_KEY, tuttiKept.dump(ui)); } catch { /* storage blocked: kept for this page only */ }
}

function loadUi() {
    try { tuttiKept.load(ui, sessionStorage.getItem(UI_KEY)); } catch { /* storage blocked: start from the defaults */ }
}

// A <details> with `keep: key` opens as the owner left it (its `open` prop is only the default) and remembers each toggle.
// The toggle event of the render's own opening is not the owner's choice: only a change from what is kept is recorded.
function keepOpen(node, key, byDefault) {
    node.open = tuttiKept.isOpen(ui, key, byDefault);
    node.addEventListener('toggle', () => {
        if (node.open === tuttiKept.isOpen(ui, key, byDefault)) return;
        tuttiKept.toggled(ui, key, node.open);
        saveUi();
    });
}

// The props without `keep` (and its `open`), the fold wired up when there is one.
function keptProps(node, props) {
    const { keep, ...rest } = props || {};
    if (!keep) return rest;
    keepOpen(node, keep, Boolean(rest.open));
    delete rest.open;
    return rest;
}

// A link built from data (a pull request's or a usage page's URL) is only ever http(s), mailto, an in-page # or a path of this
// dashboard: never javascript: or data:, whatever the data says (the CSP blocks those too; this is the second wall).
function safeHref(v) {
    const bare = v.replace(/[\u0000-\u0020]/g, '');   // browsers ignore these inside a scheme ("java script:", "\tjavascript:")
    const scheme = /^[a-z][a-z0-9+.-]*:/i.exec(bare);
    return !scheme || /^(https?|mailto):$/i.test(scheme[0]) ? v : '#';
}

function el(tag, props, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(keptProps(node, props))) {
        if (v === undefined || v === null || v === false) continue;
        if (k === 'text') node.textContent = v;
        else if (k === 'class') node.className = v;
        else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
        else if (k === 'href' || k === 'src') node.setAttribute(k, safeHref(String(v)));
        else node.setAttribute(k, v === true ? '' : String(v));
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) node.append(kid);
    return node;
}

function svg(tag, attrs) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, String(v));
    return node;
}

const chip = (text, tone) => el('span', { class: 'chip', 'data-tone': tone || 'idle', text });
const fmtTime = new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto', style: 'short' });

// A time in the viewer's own zone, and the same moment in UTC for the tooltip (the server stores and sends UTC only).
const utcTitle = (iso) => (iso ? `${new Date(iso).toISOString().slice(0, 16).replace('T', ' ')} UTC` : '');
const localTime = (iso) => (iso ? `${fmtTime.format(new Date(iso))} (${Intl.DateTimeFormat().resolvedOptions().timeZone})` : '');

function relative(iso, now = Date.now()) {
    if (!iso) return '';
    const s = (Date.parse(iso) - now) / 1000;
    const a = Math.abs(s);
    if (a < 45) return rtf.format(Math.round(s), 'second');
    if (a < 2700) return rtf.format(Math.round(s / 60), 'minute');
    if (a < 64800) return rtf.format(Math.round(s / 3600), 'hour');
    return rtf.format(Math.round(s / 86400), 'day');
}

function duration(seconds) {
    if (seconds === null || seconds === undefined) return '';
    const m = Math.round(seconds / 60);
    if (m < 1) return `${Math.round(seconds)} s`;
    if (m < 90) return `${m} min`;
    const h = Math.floor(m / 60);
    return h < 48 ? `${h} h ${m % 60} min` : `${Math.floor(h / 24)} d ${h % 24} h`;
}

const when = (iso) => (iso ? `${fmtTime.format(new Date(iso))} (${relative(iso)})` : 'not known');

function empty(title, text) {
    return el('div', { class: 'empty' }, el('strong', { text: title }), text);
}

function codeText(before, code, after) {
    return el('span', {}, before, el('code', { text: code }), after || '');
}

// ---- long lists: the first `limit` items and "Show N more" (tuttiMore in attention.js); the choice is kept across polls

function more(key, nodes, limit, tag = 'li') {
    const m = tuttiMore(nodes.length, limit, ui.expanded.has(key));
    if (!m.button) return nodes;
    const button = el('button', {
        class: 'btn btn--text more', type: 'button', 'data-more': key, 'aria-expanded': String(ui.expanded.has(key)), text: m.button,
        onclick: () => {
            if (ui.expanded.has(key)) ui.expanded.delete(key);
            else ui.expanded.add(key);
            saveUi();
            render();
            document.querySelector(`[data-more="${CSS.escape(key)}"]`)?.focus({ preventScroll: true });
        },
    });
    return [...nodes.slice(0, m.shown), el(tag, { class: 'more__row' }, button)];
}

// ---- tiles

function tile(label, value, note, tone) {
    return el('div', { class: 'tile', 'data-tone': tone }, el('p', { class: 'tile__label', text: label }),
        el('p', { class: 'tile__value', text: value }), el('p', { class: 'tile__note', text: note }));
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

function nextNote(t) {
    const n = t.next_worker;
    if (!n) return '';
    if (!n.worker) return ' · no agent account has room';
    return ` · next agent job: ${[n.worker, ...n.args].join(' ')}`;
}

function quotaTile(s) {
    const t = s.summary;
    const read = s.usage.accounts.filter((a) => a.state === 'ok').length;
    const missing = s.usage.accounts.length - read;
    if (t.panic.length) return tile('Quota', `Panic: ${t.panic.join(', ')}`, 'Nothing new on these accounts until the reset' + nextNote(t), 'bad');
    if (t.behind.length) return tile('Quota', `Behind: ${t.behind.join(', ')}`, 'Start more parallel work from the backlog' + nextNote(t), 'warn');
    if (!read) return tile('Quota', 'No readings', 'Log one: tutti usage log <account> <percent>', 'warn');
    return tile('Quota', 'On pace', (missing ? `${read} paced · ${missing} without a reading` : `${read} accounts paced`) + nextNote(t));
}

function nextTile(t) {
    const n = t.next_worker;
    if (!n) return tile('Next tasks go to', '—', 'No agent account names a worker');
    if (!n.worker) return tile('Next tasks go to', 'a local model', n.reason, 'warn');
    return tile('Next tasks go to', n.worker, `${n.account}: ${n.reason}`, 'ok');
}

function workersNote(t) {
    const runs = Object.entries(t.runs_today || {}).map(([w, n]) => `${w} ${n}`).join(', ');
    const parts = [runs ? `runs today: ${runs}` : 'no runs today'];
    if (t.outside) parts.push(`${plural(t.outside, 'agent')} started outside Tutti: use tutti run auto`);
    if (t.workers_ready < t.workers) parts.push('some are down: see Workers');
    return parts.join(' · ');
}

function renderTiles(s) {
    const t = s.summary;
    $('tiles').replaceChildren(
        nextTile(t),
        quotaTile(s),
        tile('Free slots', `${t.free_slots} / ${t.slots}`,
            t.machines_offline.length ? `Offline: ${t.machines_offline.join(', ')}` : plural(s.machines.items.length, 'machine'),
            t.machines_offline.length ? 'warn' : undefined),
        tile('Jobs', `${t.running} running`, t.stalled ? `${t.stalled} stalled: open its log` : 'none stalled', t.stalled ? 'warn' : undefined),
        tile('Workers', `${t.workers_ready} / ${t.workers} ready`, workersNote(t), t.workers_ready < t.workers || t.outside ? 'warn' : undefined),
    );
}

// ---- usage

function meter(a) {
    const used = Math.round(Math.min(100, Math.max(0, a.used ?? 0)));
    let tone;
    if (a.panic) tone = 'bad';
    else if (a.verdict === 'ahead') tone = 'warn';
    const m = el('div', {
        class: 'meter', 'data-tone': tone, role: 'meter', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': used,
        'aria-label': `${a.name}: ${tuttiPct(a.used ?? 0)} used`,
    });
    const fill = el('div', { class: 'meter__fill' });
    fill.style.width = tuttiCssPct(a.used);
    m.append(fill);
    if (a.expected !== null) {
        const mark = el('div', { class: 'meter__mark', title: `By now: ${tuttiPct(a.expected)}` });
        mark.style.left = tuttiCssPct(a.expected);
        m.append(mark);
    }
    const p = el('div', { class: 'meter__mark meter__mark--panic', title: `Panic at ${tuttiPct(a.panic_at)}` });
    p.style.left = tuttiCssPct(a.panic_at);
    m.append(p);
    return m;
}

function sparkline(a, now) {
    const t0 = Date.parse(a.window_start);
    const t1 = Date.parse(a.reset);
    const tt = Date.parse(a.target);
    const W = 300;
    const H = 72;
    const x = (t) => ((t - t0) / (t1 - t0)) * W;
    const y = (p) => H - 4 - (Math.min(100, p) / 100) * (H - 8);
    const g = svg('svg', { class: 'spark', viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', role: 'img' });
    const pts = a.readings.map((r) => [x(Date.parse(r.at)), y(r.used)]);
    g.setAttribute('aria-label', `${a.name}: ${a.readings.length} readings this window; the dashed line is the even pace to 100 % by the target time`);
    g.append(svg('line', { class: 's-grid', x1: 0, x2: W, y1: y(0), y2: y(0) }));
    g.append(svg('line', { class: 's-panic', x1: 0, x2: W, y1: y(a.panic_at), y2: y(a.panic_at) }));
    g.append(svg('polyline', { class: 's-target', points: `0,${y(0)} ${x(tt)},${y(100)} ${W},${y(100)}` }));
    const nx = x(now);
    if (nx >= 0 && nx <= W) g.append(svg('line', { class: 's-now', x1: nx, x2: nx, y1: 0, y2: H }));
    if (pts.length) {
        g.append(svg('polyline', { class: 's-line', points: [[0, y(0)], ...pts].map((p) => p.join(',')).join(' ') }));
    }
    return g;
}

function facts(pairs) {
    return el('dl', { class: 'account__facts' }, pairs.filter(([, v]) => v).flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]));
}

// ---- add a reading: opens the provider's usage page and a small form; saves through POST /api/reading

const pad = (n) => String(n).padStart(2, '0');

function localInput(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function openReading(a) {
    if (a.usage_url) window.open(a.usage_url, '_blank', 'noopener,noreferrer');
    ui.editing.set(a.name, { used: Math.round(a.used ?? 0), resets: localInput(a.reset), note: '', msg: '', tone: '' });
    renderUsage(ui.last, Date.now(), true);
    document.getElementById(`reading-${a.name}-used`)?.focus();
}

function closeReading(name) {
    ui.editing.delete(name);
    renderUsage(ui.last, Date.now(), true);
    document.querySelector(`[data-add="${CSS.escape(name)}"]`)?.focus();
}

async function saveReading(name, form) {
    const f = ui.editing.get(name);
    const body = { account: name, used: Number(f.used), note: f.note };
    if (f.resets) body.resets = new Date(f.resets).toISOString();
    try {
        const res = await fetch('/api/reading', {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN }, body: JSON.stringify(body),
        });
        const answer = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
        ui.editing.delete(name);
        await refresh();
    } catch (e) {
        f.msg = `Not saved: ${e.message}`;
        f.tone = 'bad';
        form.querySelector('.reading__msg').textContent = f.msg;
        form.querySelector('.reading__msg').dataset.tone = 'bad';
    }
}

function readingForm(a) {
    const f = ui.editing.get(a.name);
    const id = (k) => `reading-${a.name}-${k}`;
    const pct = el('output', { class: 'reading__pct', for: id('used'), text: tuttiPct(f.used) });
    const form = el('form', { class: 'reading', 'aria-label': `New reading for ${a.name}` },
        el('label', { for: id('used') }, el('span', {}, 'Used now: ', pct),
            el('input', { id: id('used'), type: 'range', min: 0, max: 100, step: 1, value: f.used,
                oninput: (e) => { f.used = e.target.value; pct.textContent = tuttiPct(f.used); } })),
        el('label', { for: id('resets') }, 'Resets at (your time; prefilled from the window rule)',
            el('input', { id: id('resets'), type: 'datetime-local', value: f.resets, oninput: (e) => { f.resets = e.target.value; } })),
        el('label', { for: id('note') }, 'Note (optional)',
            el('input', { id: id('note'), type: 'text', maxlength: 200, value: f.note, oninput: (e) => { f.note = e.target.value; } })),
        el('div', { class: 'reading__row' }, el('button', { class: 'btn btn--filled', type: 'submit', text: 'Save reading' }),
            el('button', { class: 'btn btn--tonal', type: 'button', text: 'Cancel', onclick: () => closeReading(a.name) }),
            a.usage_url ? el('a', { href: a.usage_url, target: '_blank', rel: 'noopener noreferrer', text: 'Open the usage page again' }) : null),
        el('p', { class: 'reading__msg', 'data-tone': f.tone || undefined, role: 'status', text: f.msg }));
    form.addEventListener('submit', (e) => { e.preventDefault(); void saveReading(a.name, form); });
    return form;
}

const SOURCE = { reader: 'read automatically', file: 'written by the orchestrator', helper: 'read by the helper', 'limit message': 'from a limit message' };

function autoLine(a) {
    if (a.reader_problem) return el('p', { class: 'account__auto', 'data-tone': 'bad', text: `Automatic reading failed: ${a.reader_problem}` });
    const parts = [];
    if (a.auto && a.reading_at) parts.push(`${SOURCE[a.reading_source] || 'read automatically'} · ${relative(a.reading_at)}`);
    if (a.auto_every_minutes) parts.push(`every ${a.auto_every_minutes} min, no input needed`);
    if (a.plan) parts.push(`plan: ${a.plan}`);
    return parts.length ? el('p', { class: 'account__auto', text: parts.join(' · ') }) : null;
}

function resetText(a) {
    if (!a.reset) return null;
    return a.reset_from ? when(a.reset) + ', ' + RESET_FROM[a.reset_from] : when(a.reset);
}

function accountFacts(a) {
    return facts([
        ['Resets', resetText(a)], ['Spend by', a.target && when(a.target)],
        ['Last reading', a.reading_at && when(a.reading_at)], ['Window', a.window],
        ['Share', a.share < 1 ? `${tuttiShare(a.share)} of the quota is this session's` : null], ['Read at', a.source],
    ]);
}

// The planner's plan as label/value rows (attention.js builds them), times in the viewer's time with UTC as the tooltip.
function planFacts(plan) {
    const rows = tuttiPlanRows(plan, (iso) => fmtTime.format(new Date(iso)));
    return el('dl', { class: 'account__facts account__plan', 'aria-label': 'Planner' },
        ...rows.flatMap((r) => [el('dt', { text: r.label }), el('dd', { text: r.text, ...(r.title ? { title: r.title } : {}) })]));
}

// The details of an account fold away so the cards stay compact in one row; an open one stays open across polls.
function factsToggle(a) {
    const d = el('details', { class: 'account__more', keep: `facts:${a.name}` }, el('summary', { text: 'Details' }), accountFacts(a));
    return d;
}

// One window of an account bound by a longer limit (agy: 5 h and weekly side by side), with the planner's share of it.
function windowBlock(w, label) {
    const verdict = w.state === 'ok' ? (w.panic ? 'panic' : w.verdict) : w.state;
    const box = el('div', { class: 'window' }, el('p', { class: 'window__label' }, label, ' ', chip(verdict, w.panic ? 'bad' : VERDICT_TONE[w.verdict])));
    if (w.state !== 'ok') return box;
    box.append(el('p', { class: 'window__pct' }, tuttiPct(w.used), el('span', { class: 'account__by', text: ` · ${tuttiPct(w.expected)} by now` })), meter(w));
    if (w.reset) box.append(el('p', { class: 'account__by', text: `resets ${relative(w.reset)}` }));
    return box;
}

// The work browser (docs/BROWSER.md): it reads the usage page by itself, after one sign-in there.
function browserButtons(a) {
    let site;
    try {
        site = new URL(a.usage_url).hostname;
    } catch {
        return el('p', { class: 'row__note', text: `The usage page address of ${a.name} is not a web address: fix usage_url in Settings, Accounts.` });
    }
    const go = (action, label) => el('button', {
        class: 'btn btn--text', type: 'button', text: label, 'data-browser': action,
        onclick: () => void postBrowser({ action, url: a.usage_url }, action === 'read' ? `Read ${site} on the work browser.` : ''),
    });
    return el('div', { class: 'account__browser' }, go('read', 'Read on the work browser'), go('sign-in', `Sign in to ${site} on the work browser`));
}

async function postBrowser(body, done) {
    const res = await fetch('/api/browser', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN }, body: JSON.stringify(body),
    }).catch(() => null);
    const answer = res ? await res.json().catch(() => ({})) : {};
    const problems = Object.values(answer.problems || {});
    if (!res) {
        banner('The dashboard did not answer: check that Tutti still runs, then try again.');
        return;
    }
    const said = problems.length ? problems.join(' ') : answer.message || done;
    banner(answer.error || said);
}

function addButton(a) {
    return el('button', {
        class: 'btn btn--tonal account__add', type: 'button', 'data-add': a.name, text: 'Add a reading',
        title: a.usage_url ? `Opens ${a.usage_url} and a short form` : 'Opens a short form', onclick: () => openReading(a),
    });
}

// Free room on an account: what the autopilot does with it when it fills the room by itself (autopilot.fill_room), else a
// short hint and a button that turns it on. Never a command to type.
function roomLine(a) {
    const ap = ui.autopilot;
    const trigger = ap?.accounts?.includes(a.name);
    if (ap?.enabled && ap.fill_room && trigger) {
        const running = ap.running.filter((t) => t.step === a.name).length;
        const note = ap.fill?.note ? ` · ${ap.fill.note}` : '';
        return el('p', { class: 'account__nudge', 'data-tone': 'ok',
            text: `Autopilot: filling ${plural(a.room_tasks, 'slot')} — ${running} running here, ${ap.queue.length} queued${note}` });
    }
    const hint = ap?.enabled && !trigger ? ' (not in autopilot.accounts: it takes retries only)' : '';
    return el('p', { class: 'account__nudge', 'data-tone': 'ok' }, `${plural(a.room_tasks, 'bounded task')} fit here now${hint}. `,
        hint ? '' : el('button', { class: 'btn btn--tonal', type: 'button', 'data-fill': a.name, onclick: fillRoom, text: 'Let autopilot fill it' }));
}

// A scout's account (Grok Bot): its allowance, the next watch and the last answer, never room for bounded tasks.
function scoutFacts(sc) {
    const f = sc.facts || {};
    const allowance = f.used === null || f.used === undefined ? 'allowance: no reading yet' : `allowance: ${tuttiPct(f.used)} used, ${tuttiPct(f.left)} left`;
    const next = f.next_watch ? `next watch: ${f.next_watch.name} ${when(f.next_watch.at)}` : 'no watches';
    const last = f.last_answer ? `last answer ${relative(f.last_answer.at)}: ${f.last_answer.text || f.last_answer.id}` : 'no answer yet';
    const week = sc.week?.text ? ` · this week: ${sc.week.text}` : '';
    return el('p', { class: 'account__nudge', 'data-tone': 'ok', 'data-scout': sc.name, text: `Scout ${sc.name} · ${allowance} · ${next} · ${last}${week}` });
}

async function fillRoom() {
    await switchAutopilot({ enabled: true, fill_room: true }, 'The autopilot was not switched on.');
}

function account(a, now) {
    const verdict = a.panic ? 'panic' : a.verdict;
    const card = el('article', { class: 'account', 'data-panic': String(a.panic), 'aria-label': `${a.name}: ${verdict}` },
        el('div', { class: 'account__top' }, el('h3', { class: 'account__name', text: a.name }),
            chip(verdict, a.panic ? 'bad' : VERDICT_TONE[a.verdict])));
    if (a.state === 'ok' && a.limits?.length) {
        card.append(el('div', { class: 'windows' }, windowBlock(a, `${a.window} window`), ...a.limits.map((l) => windowBlock(l, `${l.window} limit`))));
    } else if (a.state === 'ok') {
        card.append(el('div', { class: 'account__big' }, el('span', { class: 'account__pct', text: tuttiPct(a.used) }),
            el('span', { class: 'account__by', text: `used · ${tuttiPct(a.expected)} by now on an even pace` })));
        card.append(meter(a));
        card.append(el('div', { class: 'legend' }, el('span', {}, el('i', { class: 'l-used' }), 'used'),
            el('span', {}, el('i'), 'pace line (by now)'), el('span', {}, el('i', { class: 'l-panic' }), `panic at ${tuttiPct(a.panic_at)}`)));
    } else {
        card.append(el('p', { class: 'account__by', text: a.state === 'no reset' ? 'No reset time in the config yet.' : 'No reading in this window yet.' }));
    }
    if (a.reset) card.append(sparkline(a, now));
    card.append(el('p', { class: 'account__action', text: `Do: ${tuttiTidy(a.action)}` }));
    if (a.quota_plan?.rows?.length) card.append(planFacts(a.quota_plan));
    else if (a.quota_plan?.why) card.append(el('p', { class: 'account__plan', text: `Planner: ${a.quota_plan.why}` }));
    if (a.outlook) {
        card.append(el('p', { class: 'account__auto', 'data-target': a.name, 'data-tone': a.outlook.short ? 'warn' : 'ok', text: tuttiTidy(a.outlook.text) }));
    } else if (a.projected_end !== undefined && a.projected_end !== null) {
        card.append(el('p', { class: 'account__auto', 'data-tone': a.projected_end < 90 ? 'warn' : 'ok',
            text: `On track to finish at ${tuttiPct(a.projected_end)} by its reset (the last day's rate carried on)` }));
    }
    const scout = (ui.scouts?.items || []).find((i) => i.account === a.name);
    if (scout) card.append(scoutFacts(scout));   // a scout answers questions: no task room on its card
    else if (a.room_tasks) card.append(roomLine(a));
    if (a.credits_used !== undefined) card.append(el('p', { class: 'account__auto', text: `${a.credits_used} AI credits in this window from Tutti's runs` }));
    if (a.stale) card.append(el('p', { class: 'account__remind', 'data-tone': 'warn', role: 'note', text: `Reminder: ${a.remind}` }));
    card.append(factsToggle(a), autoLine(a) || '');
    // The account's note is a warning callout right above Add a reading (icon and the word "Note", never colour alone).
    if (a.note) {
        card.append(el('div', { class: 'account__callout', role: 'note', 'data-tone': 'warn' },
            el('span', { class: 'account__callout-icon', 'aria-hidden': 'true', text: '⚠' }),
            el('p', {}, el('strong', { text: 'Note: ' }), a.note)));
    }
    // Add a reading only where it is needed: nothing reads the account on its own, or its reading is old (a form being typed stays).
    if (ui.editing.has(a.name)) card.append(readingForm(a));
    else if (a.needs_reading !== false) card.append(addButton(a));
    if (a.tutti_browser && !ui.editing.has(a.name)) card.append(browserButtons(a));
    return card;
}

function renderUsage(s, now, force) {
    const u = s.usage;
    // While a reading is being typed, the cards stay as they are: a poll must not wipe the form or move the focus.
    if (ui.editing.size && !force && !ui.formPending) {
        $('usage-meta').textContent = 'Paused while you add a reading';
        return;
    }
    ui.formPending = false;
    const stale = u.accounts.filter((a) => a.stale).length;
    $('usage-meta').textContent = u.problem || (stale ? `${plural(stale, 'account')} need a fresh reading · ` : '') + `Readings from ${u.log}`;
    // An account that is another's limit (agy's weekly) shows inside that account's card, beside its 5 h window.
    // A limit that needs a reading (or has its form open) keeps its own card too: Add a reading lives there.
    const byName = new Map(u.accounts.map((a) => [a.name, a]));
    const ownCard = (n) => ui.editing.has(n) || byName.get(n)?.needs_reading !== false;
    const inside = new Set(u.accounts.flatMap((a) => (a.limits || []).map((l) => l.name)).filter((n) => !ownCard(n)));
    const shown = u.accounts.filter((a) => !inside.has(a.name));
    $('usage').replaceChildren(...(shown.length ? shown.map((a) => account(a, now))
        : [empty('No accounts yet', codeText('Add an accounts: section to the config (see ', 'tutti.config.example.yaml', ').'))]));
    if (u.owner) $('owner').replaceChildren(...ownerPanel(u.owner));
}

// ---- the owner's own prompting: the mode, "I'm away until…", and the learned week (tutti/owner.py)

const MODE_TEXT = { auto: 'auto: working days by the profile', active: 'active: the reserve is kept', away: 'away for the rest of today',
    vacation: 'on vacation' };

async function saveOwner(body, failText) {
    try {
        const res = await fetch('/api/owner', {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN }, body: JSON.stringify(body),
        });
        const answer = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
        await refresh();
    } catch (err) {
        showBanner(el('strong', { text: `${failText} ` }), err.message);
    }
}

function heatCell(share, kept, title) {
    const cell = el('span', { class: 'heat__cell', role: 'cell', 'data-reserve': String(kept), title, 'aria-label': title });
    cell.style.setProperty('--v', share.toFixed(2));   // CSSOM, not a style attribute: the page's CSP allows only its own styles
    return cell;
}

function heatmap(o) {
    // 7 x 24: each cell's shade is the owner's points per hour; the planner's reserve hours are outlined.
    const top = Math.max(0.001, ...o.profile.flat());
    const rows = o.profile.map((row, d) => el('div', { class: 'heat__row', role: 'row' },
        el('span', { class: 'heat__day', role: 'rowheader', text: o.days[d] }),
        ...row.map((v, h) => heatCell(v / top, o.reserve[d][h] > 0, `${o.days[d]} ${String(h).padStart(2, '0')}:00 · ${v} points/h${o.reserve[d][h] > 0 ? ' · kept for you' : ''}`))));
    const hours = el('div', { class: 'heat__row heat__hours', 'aria-hidden': 'true' }, el('span', { class: 'heat__day' }),
        ...Array.from({ length: 24 }, (_, h) => el('span', { class: 'heat__cell heat__label', text: h % 6 === 0 ? String(h) : '' })));
    return el('div', { class: 'heat', role: 'table', 'aria-label': `Your week: points per hour by weekday and hour (your local time, ${o.zone || 'this computer\'s zone'}); outlined hours are kept for you` },
        ...rows, hours);
}

// The browser's zone, sent once when the dashboard has another one: Tutti uses it only when it cannot detect the host's
let zoneSent = false;
function sendBrowserZone(o) {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (zoneSent || !zone || o.browser_zone === zone) return;
    zoneSent = true;
    fetch('/api/owner', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN },
        body: JSON.stringify({ browser_zone: zone }) })
        .then((res) => { if (!res.ok) zoneSent = false; })   // a failed save tries again on the next refresh
        .catch(() => { zoneSent = false; });
}

function ownerPanel(o) {
    sendBrowserZone(o);
    const until = o.until ? ` until ${localTime(o.until)}` : '';
    const t = new Date(Date.now() + 86400000);   // tomorrow, local: the server reads the date as the owner's local midnight
    const date = el('input', { type: 'date', class: 'field', 'aria-label': 'Away until (date)',
        value: `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, '0')}-${String(t.getDate()).padStart(2, '0')}` });
    const dDate = el('input', { type: 'date', class: 'field', 'aria-label': 'A day with other hours (date)', value: date.value });
    const dWin = el('input', { type: 'text', class: 'field', 'aria-label': 'Its hours, HH-HH or off', placeholder: '20-24', size: 6 });
    const dated = Object.entries(o.dates || {}).map(([d, w]) => el('span', { class: 'chip' }, `${d}: ${w === 'off' ? 'off' : w}`,
        el('button', { class: 'btn btn--text', type: 'button', text: 'Remove', 'aria-label': `Remove the hours of ${d}`,
            onclick: () => saveOwner({ date: d, window: null }, 'The day was not saved.') })));
    const learned = o.learned ? `learned from ${o.hours_seen} h of readings` : `seeded from pacing.schedule (${o.schedule.window}, ${o.schedule.hours} h)`;
    return [
        el('h3', { class: 'card__sub', text: 'You and the main agent ' }, el('a', { class: 'link', href: '#settings/pacing', text: 'In Settings' })),
        el('p', { class: 'row__note', title: utcTitle(o.until), text: `Mode: ${MODE_TEXT[o.effective] || o.effective}${until} (set: ${o.mode}) · ${o.accounts.join(', ') || 'no account'} · ${learned}` }),
        o.today ? el('p', { class: 'row__note', 'data-owner-today': '', text: o.today }) : '',
        el('div', { class: 'row__stats' },
            date,
            el('button', { class: 'btn btn--tonal', type: 'button', text: 'I\'m away until…',
                onclick: () => saveOwner({ mode: `vacation-until-${date.value}` }, 'The mode was not saved.') }),
            el('button', { class: 'btn btn--tonal', type: 'button', text: 'Away today', onclick: () => saveOwner({ mode: 'away' }, 'The mode was not saved.') }),
            el('button', { class: 'btn btn--tonal', type: 'button', text: 'I\'m back (auto)', onclick: () => saveOwner({ mode: 'auto' }, 'The mode was not saved.') })),
        heatmap(o),
        el('div', { class: 'row__stats' }, el('span', { class: 'row__note', text: 'One day with other hours (the reserve follows them that day):' }),
            dDate, dWin, el('button', { class: 'btn btn--tonal', type: 'button', text: 'Save that day',
                onclick: () => saveOwner({ date: dDate.value, window: dWin.value.trim() || 'off' }, 'The day was not saved.') }), ...dated),
        el('div', { class: 'row__stats' }, el('span', { class: 'row__note', text: 'Your working days (the reserve is kept on them):' }), ...o.days.map((d) => el('button', {
            class: 'btn btn--tonal', type: 'button', 'aria-pressed': String(o.schedule.days.includes(d)), title: `Your working days: ${d}`,
            text: d, onclick: () => saveOwner({ days: o.schedule.days.includes(d) ? o.schedule.days.filter((x) => x !== d) : [...o.schedule.days, d] },
                'The working days were not saved.'),
        }))),
    ];
}

// ---- machines

// One box per slot: busy (filled), held back by its share or its yield flag (crossed), free (empty)
function pips(m) {
    const held = m.held_back || 0;
    const label = m.free === null ? `${m.slots} slots, not reachable`
        : `${m.busy} busy, ${m.free} free${held ? `, ${held} held back` : ''} of ${m.slots} slots`;
    return el('span', { class: 'pips', role: 'img', 'aria-label': label },
        ...Array.from({ length: m.slots }, (_, i) => el('span', { class: 'pip', 'data-busy': String(m.free !== null && i < m.busy),
            'data-held': String(m.free !== null && i >= m.busy && i < m.busy + held), 'data-off': String(m.free === null) })));
}

function slotText(m) {
    if (m.free === null) return `${plural(m.slots, 'slot')}`;
    const held = m.held_back ? ` (${m.held_back} held back: ${m.use_why || `share ${tuttiPct(m.use_pct)}`})` : '';
    return `${m.free} of ${plural(m.slots, 'slot')} free${held}`;
}

function machine(m) {
    const stats = el('div', { class: 'row__stats' }, pips(m), el('span', { title: `Slots: ${m.slots_why || ''}`, text: slotText(m) }));
    if (m.load !== null) {
        const bar = el('span');
        bar.style.width = tuttiCssPct((m.load / m.cores) * 100);
        stats.append(el('span', { class: 'load' }, el('span', { class: 'load__bar', 'aria-hidden': 'true' }, bar), `load ${tuttiNum(m.load)} / ${m.cores} cores`));
    }
    for (const [label, t] of [['CPU', m.temp_c], ['GPU', m.gpu_temp_c]]) {
        if (t === null || t === undefined) continue;
        const hot = m.heat?.cap_c !== undefined && t >= m.heat.cap_c;
        stats.append(el('span', { class: 'temp', 'data-tone': hot ? 'warn' : undefined, text: `${label} ${tuttiNum(t, { digits: 0 })} °C` }));
    }
    if ((m.cast_roles || []).length) stats.append(el('span', { text: 'Cast sender', title: 'Chrome, Chromium or the desktop sender (checked daily; machines.<m>.roles overrides)' }));
    if (m.heat?.cap_c !== undefined) stats.append(el('span', { class: 'temp', title: 'machines.<m>.heat (Settings): one run at the cap, none at the stop',
        text: `heat cap ${m.heat.cap_c} °C · stop ${m.heat.stop_c ?? '–'} °C` }));
    if (m.temp_note && (m.temp_c === null || m.temp_c === undefined)) stats.append(el('span', { class: 'temp', title: m.temp_note,
        text: /LibreHardwareMonitor/.test(m.temp_note) ? 'CPU °C: start LibreHardwareMonitor on Windows to read it' : 'CPU °C not readable' }));
    if (m.rank) stats.append(el('span', { text: `#${m.rank} in routing` }));
    if (m.use_pct !== undefined && m.use_pct !== null) stats.append(el('span', { title: 'machines.<m>.use_pct (Settings)',
        text: `Tutti's share ${tuttiPct(m.use_pct)}${m.use_why ? ` (${m.use_why})` : ''}` }));
    if (m.slots_why && !/\.slots$/.test(m.slots_why)) stats.append(el('span', { class: 'row__note', text: `${plural(m.slots, 'slot')}: ${m.slots_why}` }));
    const use = (ui.last?.household_use?.machines || []).find((u) => u.name === m.name);
    if (use && use.hour !== null) stats.append(el('span', { text: `used ${tuttiPct(use.hour)} last hour · ${tuttiPct(use.day)} today` }));
    return el('li', { class: 'row' },
        el('div', { class: 'row__name' }, m.name, el('span', { class: 'row__kind', text: `${m.transport} · ${m.host}` })),
        chip(m.status, MACHINE_TONE[m.status]),
        stats,
        presenceBits(m.presence),   // controls.js: Here / Away since 18:40 (left with its owner), today's strip
        el('p', { class: 'row__note', text: m.status === 'paused' ? 'Yield flag is up: Tutti sends it nothing new until it is lowered.' : m.reason }),
        use && use.idle_why ? el('p', { class: 'row__note', text: `Idle: ${use.idle_why}` }) : null,
        limitForm(m),
        m.household_use ? el('p', { class: 'row__note household-only', text: busyFor(m) }) : null,
        storedFiles(m.name, m.stored));
}

// A machine's limited-usage window (machines.<m>.limit, tutti/share.py): a daily window when Tutti uses at most pct % of it
function limitForm(m) {
    const lim = m.limit || {};
    const win = el('input', { type: 'text', class: 'field', size: 8, placeholder: 'none', value: lim.window || '',
        'aria-label': `${m.name}: limited hours, HH-HH` });
    const pct = el('input', { type: 'number', class: 'field', min: 1, max: 100, step: 5, value: String(lim.pct ?? 60),
        'aria-label': `${m.name}: the most Tutti uses then, percent` });
    const save = (body, fail) => fetch('/api/machine-limit', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN },
        body: JSON.stringify(body) }).then(async (res) => {
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `the dashboard answered ${res.status}`);
        await refresh();
    }).catch((err) => showBanner(el('strong', { text: `${fail} ` }), err.message));
    return el('div', { class: 'row__stats' },
        el('span', { class: 'row__note', text: lim.window ? `Limited hours ${lim.window} at ${lim.pct ?? 60} %${m.limited ? ' (now)' : ''}:`
            : 'Limited hours: none set (type HH-HH, e.g. 18-01, to add):' }),
        win, pct, el('span', { text: '%' }),
        el('button', { class: 'btn btn--tonal', type: 'button', text: 'Save',
            onclick: () => save({ machine: m.name, window: win.value.trim(), pct: pct.value.trim() === '' ? null : Number(pct.value) },
                'The limited hours were not saved.') }),
        lim.window ? el('button', { class: 'btn btn--text', type: 'button', text: 'No limit',
            onclick: () => save({ machine: m.name, window: 'off' }, 'The limited hours were not removed.') }) : null);
}

function bytes(n) {
    const units = [['GB', 1024 ** 3], ['MB', 1024 ** 2], ['KB', 1024]];
    const [unit, size] = units.find(([, s]) => n >= s) || ['B', 1];
    return `${size === 1 ? tuttiNum(n, { digits: 0 }) : tuttiNum(n / size, { digits: 1 })} ${unit}`;
}

// The files `tutti files send` keeps in the machine's cache: what, how big, last used, against the budget.
function storedFiles(name, st) {
    if (!st) return null;
    if (st.problem) return el('p', { class: 'row__note', text: `Stored files: ${st.problem}` });
    const head = `Stored files: ${bytes(st.bytes)} of ${bytes(st.budget_bytes)} in ${st.cache}`;
    if (!st.items.length) return el('p', { class: 'row__note', text: `${head}. None yet: tutti files send <path> --to <machine>` });
    const d = el('details', { class: 'stored', keep: `stored:${name}`, 'data-machine': name });
    d.append(el('summary', { text: `${head} · ${st.items.length} item${st.items.length === 1 ? '' : 's'}` }),
        el('ul', { class: 'stored__list' }, ...st.items.map((it) => el('li', {},
            el('span', { class: 'stored__name', text: it.name }),
            el('span', { text: `${bytes(it.bytes)} · used ${relative(new Date(it.used_at * 1000).toISOString())}` })))));
    return d;
}

// what self-healing repaired today (tutti/selfheal.py, docs/SELFHEAL.md): a count, the details folded
function renderSelfHeal(h) {
    const box = $('machines-selfheal');
    if (!box || !h) return;
    const tidied = h.tidied ? ` · ${h.tidied} routine clean-up${h.tidied === 1 ? '' : 's'} (finished runs' helpers, old temp folders)` : '';
    const head = `Self-healed: ${plural(h.today, 'repair')} today${tidied}`;
    const rows = [...h.items, ...(h.tidy_items || [])];
    if (!rows.length) { box.replaceChildren(el('p', { class: 'row__note', text: `${head}. Nothing needed repairing.` })); return; }
    const d = el('details', { class: 'stored', keep: 'selfheal' });
    d.append(el('summary', { text: head }), el('ul', { class: 'stored__list' }, ...rows.map((r) => el('li', {},
        el('span', { class: 'stored__name', text: `${r.host} · ${r.check}` }),
        el('span', { text: `${r.what} · ${relative(new Date(r.at * 1000).toISOString())}` })))));
    const hadFocus = box.contains(document.activeElement);
    box.replaceChildren(d);
    if (hadFocus) d.querySelector('summary')?.focus({ preventScroll: true });   // keyboard users keep their place
}

function renderMachines(s) {
    const ms = s.machines;
    $('machines-meta').textContent = ms.probed_at ? `Probed ${relative(ms.probed_at)}` : 'Probing…';
    // this computer's disks (tutti/diskroom.py): low space or file slots stop new work; the fix in plain words
    // the backup main machine (docs/FAILOVER.md): who serves now and who takes over; an admin picks it in Settings, Machines
    const fo = s.failover;
    const foLine = $('machines-failover');
    if (foLine && fo) {
        foLine.textContent = fo.backup
            ? `Main machine: ${fo.main}. Backup: ${fo.backup}${fo.chosen ? '' : ' (picked by Tutti)'}. Serving now: ${fo.active}.${fo.may_write ? '' : ' This machine is read-only until it catches up.'}`
            : `Main machine: ${fo.main}. No backup main machine yet: pick one in Settings, Machines, or let Tutti pick.`;
        foLine.dataset.tone = fo.may_write ? '' : 'bad';
    }
    renderSelfHeal(s.selfheal);
    $('machines-disk').replaceChildren(...(s.disk || []).map((t) => el('p', { class: 'row__note', 'data-tone': 'bad', role: 'alert', text: t })));
    // A re-render replaces the rows: the stored-files toggle that had focus gets it back (keyboard users keep their place).
    const focused = document.activeElement?.closest?.('.stored')?.dataset.machine;
    $('machines').replaceChildren(...(ms.items.length ? ms.items.map(machine)
        : [el('li', {}, ms.probed_at ? empty('No machines', 'Add machines: to the config to route heavy runs.') : empty('Probing machines…', 'ssh answers can take a few seconds.'))]));
    if (focused) $('machines').querySelector(`.stored[data-machine="${CSS.escape(focused)}"] > summary`)?.focus({ preventScroll: true });
}

// ---- the work browser (docs/BROWSER.md): the household's one Chrome; its tabs per user or worker, the queue, each site's sign-in
const BROWSER_MODE = { existing: 'your own Chrome, with its sign-ins', dedicated: 'Tutti\'s own Chrome profile', isolated: 'a sandboxed Chrome of its own' };

const SIGN_IN_CHIP = { true: ['signed in', 'ok'], false: ['not signed in', 'bad'], null: ['not checked', undefined], failed: ['check failed', 'bad'] };

function renderWorkBrowser(s) {
    const w = s.work_browser;
    if (!w) return;
    $('work-browser-meta').textContent = `${w.machine} · ${BROWSER_MODE[w.mode] || w.mode}`;
    const byWho = {};
    for (const t of w.tabs) byWho[t.who] = (byWho[t.who] || 0) + 1;
    const tabs = Object.entries(byWho).map(([who, n]) => el('li', { class: 'row' }, el('div', { class: 'row__name', text: who }), chip(`${n} tab${n > 1 ? 's' : ''}`, 'ok')));
    const sites = w.sites.map((x) => el('li', { class: 'row' },
        el('div', { class: 'row__name', text: x.site }),
        chip(...(x.problem ? SIGN_IN_CHIP.failed : SIGN_IN_CHIP[String(x.signed_in)] || SIGN_IN_CHIP.null)),
        x.problem ? el('p', { class: 'row__note', text: x.problem }) : null,
        x.signed_in === true ? null : el('button', {
            class: 'btn btn--text', type: 'button', 'data-browser': 'sign-in', text: `Sign in to ${x.site} on the work browser`,
            onclick: () => void postBrowser({ action: 'sign-in', url: x.url }, ''),
        })));
    $('work-browser').replaceChildren(...[
        w.problem ? el('p', { class: 'row__note', text: w.problem }) : null,
        el('p', { class: 'row__note', text: `${w.tabs.length} of ${w.max_tabs} tabs open · ${w.queue.length} waiting` }),
        tabs.length ? el('ul', { class: 'list', 'aria-label': 'Open tabs by user or worker' }, ...tabs) : null,
        w.queue.length ? el('ul', { class: 'list', 'aria-label': 'Waiting tasks' }, ...w.queue.map((q) => el('li', { class: 'row' }, el('div', { class: 'row__name', text: q.who }), el('p', { class: 'row__note', text: q.site })))) : null,
        el('h3', { class: 'card__sub', text: 'Sites to sign in to' }),
        sites.length ? el('ul', { class: 'list', 'aria-label': 'Sign-in per site' }, ...sites) : empty('No sites yet', 'An admin adds them in Settings, Work browser.'),
        el('button', { class: 'btn btn--tonal', type: 'button', 'data-browser': 'check', text: 'Check sign-ins', onclick: () => void postBrowser({ action: 'check' }, 'Sign-ins checked.') }),
        el('p', { class: 'row__note', text: 'Sites signed in here are shared by everyone who uses the work browser; admins choose them.' }),
    ].filter(Boolean));
}

// ---- workers

function worker(w, accounts) {
    const acc = accounts.find((a) => a.name === (w.account || ''));
    return el('li', { class: 'row' },
        el('div', { class: 'row__name' }, w.name, el('span', { class: 'row__kind', text: w.kind === 'ollama' ? `local model · ${w.model}` : `cli · ${w.command}` })),
        chip(w.ready ? 'ready' : 'down', w.ready ? 'ok' : 'bad'),
        el('p', { class: 'row__note', text: w.note }),
        acc ? el('div', { class: 'row__stats' }, accountChip(acc)) : null);
}

function accountChip(acc) {
    if (acc.panic) return chip('account: panic', 'bad');
    if (acc.state === 'no reset') return chip('account: reset not set', 'idle');
    return chip(`account: ${acc.verdict}`, VERDICT_TONE[acc.verdict]);
}

function agent(a) {
    let state = 'not installed here';
    if (a.installed) state = a.configured ? 'installed · a worker' : 'installed · not in the config';
    return el('li', { class: 'agent', 'data-tone': a.installed ? 'ok' : undefined }, el('b', { text: a.label }), el('span', { text: state }));
}

function renderWorkers(s) {
    const w = s.workers;
    $('workers-meta').textContent = w.probed_at ? `Checked ${relative(w.probed_at)}` : 'Checking…';
    $('workers').replaceChildren(...(w.items.length ? w.items.map((x) => worker(x, s.usage.accounts))
        : [el('li', {}, w.probed_at ? empty('No workers', 'Add workers: to the config (cursor, agy, local models).') : empty('Checking workers…', ''))]));
    $('agents').replaceChildren(...w.agents.map(agent));
    const off = $('workers-off');
    if (off) off.replaceChildren(...(w.off || []).map((t) => el('li', { class: 'row__note', text: `Off: ${t}` })));
}

// ---- versions (tutti update)

const UPDATE_TONE = { updated: 'ok', current: 'ok', checked: 'ok', package: 'ok', busy: 'warn', owner: 'warn', manual: 'warn', 'package-old': 'warn', failed: 'bad' };
const UPDATE_TEXT = { updated: 'updated', current: 'up to date', checked: 'installed', busy: 'skipped: running', owner: 'needs the owner',
    manual: 'no updater', failed: 'failed', package: 'newest', 'package-old': 'update waiting' };

function versionRow(x) {
    const ver = x.after && x.after !== x.before ? `${x.before || '?'} → ${x.after}` : (x.before || '');
    const note = ['failed', 'busy', 'manual'].includes(x.status) ? x.detail : '';
    return el('li', { class: 'row' }, el('div', { class: 'row__name' }, x.name, el('span', { class: 'row__kind', text: ver })),
        chip(UPDATE_TEXT[x.status] || x.status, UPDATE_TONE[x.status]), note ? el('p', { class: 'row__note', text: note }) : null);
}

function versionMachine(m) {
    const rows = m.items.filter((x) => x.status !== 'missing');
    return el('li', {}, el('h4', { class: 'card__sub', text: m.name }),
        m.error ? el('p', { class: 'row__note', text: m.error }) : null,
        m.note ? el('p', { class: 'row__note', text: m.note }) : null,
        rows.length ? el('ul', { class: 'list' }, ...rows.map(versionRow)) : null);
}

function ownerStep(o) {
    return el('li', { class: 'row' }, el('div', { class: 'row__name' }, `Needs you on ${o.machine}`, el('span', { class: 'row__kind', text: o.why })),
        chip('run in a terminal', 'warn'), el('p', { class: 'row__note' }, el('code', { text: o.command })));
}

function renderVersions(s) {
    const u = s.updates;
    if (!u) return;
    const when = u.at ? `Last round ${relative(new Date(u.at * 1000).toISOString())}` : 'No round yet';
    const auto = u.auto ? `every ${u.every_hours} h automatically` : 'automatic round off (update.auto: false)';
    $('versions-meta').textContent = [u.running ? 'Updating now…' : when, auto, u.error].filter(Boolean).join(' · ');
    $('update-now').disabled = Boolean(u.running);
    $('owner-steps').replaceChildren(...u.owner.map(ownerStep));
    const models = u.models.length ? [el('li', {}, el('h4', { class: 'card__sub', text: 'local models' }), el('ul', { class: 'list' }, ...u.models.map(versionRow)))] : [];
    $('versions').replaceChildren(...u.machines.map(versionMachine), ...models);
}

async function updateNow() {
    try {
        const res = await fetch('/api/update', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN }, body: '{}' });
        const answer = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
        await refresh();
    } catch (err) {
        showBanner(el('strong', { text: 'The update did not start. ' }), err.message);
    }
}

// ---- projects

function machineLine(m) {
    const parts = [`${m.name}: ${m.processes} process${m.processes === 1 ? '' : 'es'}`];
    if (m.agents) parts.push(`${m.agents} agent${m.agents === 1 ? '' : 's'}`);
    if (m.programs?.length) parts.push(m.programs.slice(0, 5).join(', '));
    if (m.yield_flag) parts.push('its yield flag is up (the machine is this project\'s)');
    if (m.gpu) {
        const used = Math.round(m.gpu.mem_used_mib / 1024);
        const total = Math.round(m.gpu.mem_total_mib / 1024);
        parts.push(`GPU ${tuttiPct(m.gpu.util_pct)} · ${used}/${total} GB`);
    }
    return parts.join(' · ');
}

function prLine(pr) {
    const what = pr.release ? `Release ${pr.release} in progress` : (pr.draft ? 'Draft pull request' : 'Pull request');
    const ci = { failing: 'CI failing', pending: 'CI running', passing: 'CI passing', none: 'no CI' }[pr.ci] || pr.ci;
    return el('p', { class: 'row__note' },
        el('a', { href: pr.url, target: '_blank', rel: 'noopener noreferrer', text: `${what}: #${pr.number} ${pr.title}` }),
        ` · ${ci} (${pr.ci_detail}) · review: ${pr.review}`);
}

function prLines(prs) {
    if (!prs) return [];
    const lines = prs.items.map(prLine);
    for (const r of (prs.releases || []).slice(0, 1)) {
        const when = r.published ? ' (' + r.published.slice(0, 10) + ')' : '';
        lines.push(el('p', { class: 'row__note', text: `Latest release: ${r.name}${when}${r.draft ? ', a draft' : ''}` }));
    }
    if (prs.error) lines.push(el('p', { class: 'row__note', text: `Pull requests of ${prs.repo} not read: ${prs.error}` }));
    return lines;
}

function project(p) {
    const busy = p.running + p.machines.reduce((n, m) => n + m.processes, 0);
    const repos = p.repos.map((r) => [`${r.machine}: ${r.path}`, r.branch && `(${r.branch})`].filter(Boolean).join(' '));
    const plural = (n, word) => (n === 1 ? `1 ${word}` : `${n} ${word}s`);
    const counts = [plural(p.running, 'running job'), plural(p.agents, 'agent'), plural(p.runs, 'run'),
        p.conductor && `conducted by ${p.conductor}`].filter(Boolean).join(' · ');
    return el('li', { class: 'row' },
        el('div', { class: 'row__name' }, p.name, el('span', { class: 'row__kind', text: counts })),
        chip(busy ? 'active' : 'idle', busy ? 'accent' : 'idle'),
        el('p', { class: 'row__note', text: p.last_activity ? `Last activity ${relative(p.last_activity)}` : 'No activity seen' }),
        ...prLines(p.prs),
        ...repos.map((r) => el('p', { class: 'row__note', text: r })),
        ...p.machines.map((m) => el('p', { class: 'row__note', text: machineLine(m) })),
        el('div', { class: 'row__stats' }, el('button', {
            class: 'btn btn--tonal view-ok', type: 'button', text: 'Show its jobs',
            onclick: () => { ui.project = p.name; render(); $('running-h').scrollIntoView({ behavior: 'smooth' }); },
        })));
}

function renderProjects(s) {
    const ps = s.projects;
    if (!ps) return;
    const errors = Object.entries(ps.errors || {}).map(([m, e]) => `${m}: ${e}`);
    $('projects-meta').textContent = errors.length ? `Not looked at: ${errors.join('; ')}` : `${ps.items.length} seen on your machines`;
    $('projects').replaceChildren(...(ps.items.length ? more('projects', ps.items.map(project), 6)
        : [el('li', {}, empty('No projects seen yet', 'Jobs and processes inside a git repository show their project here.'))]));
}

// ---- the main agent

function mainRun(m) {
    const r = m.running || m.last;
    if (!r) return empty('No conductor run yet', 'Start one with tutti conduct "<goal>"; it shows here.');
    const turns = (r.agents || []).map((t) => [t.agent, t.stopped && `(stopped: ${t.stopped})`].filter(Boolean).join(' ')).join(' → ');
    let tone = 'warn';
    if (m.running) tone = 'accent';
    else if (r.status === 'done') tone = 'ok';
    return el('div', { class: 'row' },
        el('div', { class: 'row__name' }, m.running ? 'Running' : 'Last run', el('span', { class: 'row__kind', text: r.goal })),
        chip(r.status, tone),
        el('p', { class: 'row__note', text: `${turns || 'no agent started'} · since ${relative(r.started)}` }));
}

function renderMain(s) {
    const m = s.main;
    if (!m) return;
    $('main-meta').textContent = m.chosen ? `${m.agent} · switched here` : `${m.agent} · from the config`;
    const pick = $('main-pick');
    if (document.activeElement !== pick) {
        pick.replaceChildren(...m.choices.map((c) => el('option', {
            value: c.name, selected: c.name === m.agent,
            text: [`${c.name} (${c.kind === 'ollama' ? 'local model' : 'cli'})`, c.out && `out: ${c.out}`].filter(Boolean).join(' · '),
        })));
    }
    $('main-run').replaceChildren(mainRun(m));
}

async function switchMain(e) {
    e.preventDefault();
    const agent = $('main-pick').value;
    try {
        const res = await fetch('/api/main', {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN }, body: JSON.stringify({ agent }),
        });
        const answer = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
        ui.notice = null;
        await refresh();
    } catch (err) {
        showBanner(el('strong', { text: 'The main agent was not switched. ' }), err.message);
    }
}

// ---- the autopilot

const TASK_TONE = { queued: 'idle', running: 'accent', review: 'ok', 'needs-person': 'bad' };
const AP_GROUPS = [['running', 'Running', 6], ['queue', 'Queue', 5]];   // results to review and parked tasks are under Needs you

function attemptHistory(t) {
    if (!t.history.length) return null;
    return el('details', { keep: `history:${t.id}` },
        el('summary', { text: `${t.attempt_count} attempt(s): failure history` }),
        el('ol', { class: 'list' }, ...t.history.map((h) => el('li', {
            text: `${h.step}: ${h.ok ? 'passed' : h.why} (run ${h.run}, signature ${h.signature})`,
        }))));
}

function taskNote(t) {
    if (t.status === 'running') {
        const credits = t.credits ? ` · ${t.credits} credits` : '';
        const choice = t.choice?.label ? ` · ${t.choice.label}` : '';   // the model and effort its pace chose, and why
        return `run ${t.run} on ${t.step}${choice} · ${duration(t.elapsed)}${credits} · ${t.last_line || 'started'}`;
    }
    if (t.status === 'needs-person') return `Needs a person: ${t.parked}`;
    if (t.status === 'review' && t.landing) return `The reviewer is reading it (since ${relative(new Date(t.landing.at * 1000).toISOString())})`;
    if (t.status === 'review') return t.publish_note || `branch ${t.branch}`;
    return `${t.source} · size ${t.size}`;
}

function apTask(t) {
    return el('li', { class: 'row' },
        el('div', { class: 'row__name', title: t.title }, t.title.length > 110 ? `${t.title.slice(0, 110)}…` : t.title,
            el('span', { class: 'row__kind', text: `${t.id} · ${t.repo}` })),
        chip(`${t.status} · ${t.attempt_count} attempt(s)`, TASK_TONE[t.status] || 'idle'),
        el('p', { class: 'row__note', text: taskNote(t) }),
        attemptHistory(t));
}

function renderAutopilot(s) {
    const ap = s.autopilot;
    if (!ap) return;
    const toggle = $('autopilot-toggle');
    toggle.textContent = ap.enabled ? 'Turn off' : 'Turn on';
    toggle.setAttribute('aria-pressed', String(ap.enabled));
    $('autopilot-meta').textContent = `${ap.enabled ? 'on' : 'off'} · ${ap.ladder.join(' → ') || 'no ladder'} · ${ap.parallel_line || 'parallel limit not measured yet'}${ap.max_parallel ? ` (cap ${ap.max_parallel})` : ''}`
        + ` · fills free room: ${ap.fill_room ? 'on' : 'off'} · reviews results: ${ap.auto_review ? 'on' : 'off'}`;
    const last = ap.last;
    let stateText = 'Not set up: add autopilot.repos to the config.';
    if (last) stateText = `${last.state}: ${last.reason} (${relative(new Date(last.at * 1000).toISOString())})`;
    else if (ap.configured) stateText = 'Not run yet: turn it on; it checks after every probe.';
    $('autopilot-state').textContent = stateText;
    const groups = AP_GROUPS.filter(([key]) => ap[key].length).map(([key, title, limit]) => el('section', { class: 'autopilot__group' },
        el('h3', { class: 'card__sub', text: `${title} (${ap[key].length})` }),
        el('ul', { class: 'list' }, ...more(`ap:${key}`, ap[key].map(apTask), limit))));
    const rv = ap.resolver;   // work waits and no one takes it: the fixes Tutti tried, then the owner's one action
    if (rv) {
        const act = rv.action;
        groups.push(el('section', { class: 'autopilot__group autopilot__group--wide' },
            el('h3', { class: 'card__sub', text: 'Why nothing starts, and what Tutti tried' }),
            el('ul', { class: 'list' }, ...(rv.attempts || []).map((a) => el('li', { class: 'row' },
                el('div', { class: 'row__name', text: a.fix }), chip(a.done ? 'done' : 'no', a.done ? 'ok' : 'idle'),
                el('p', { class: 'row__note', text: a.note })))),
            act ? el('p', { class: 'row__note', text: act.why }) : null,
            act && act.kind === 'allow' ? el('button', { class: 'btn btn--filled', type: 'button', text: act.label,
                onclick: () => switchAutopilot({ allow: act.account }, `${act.account} was not allowed.`) })
                : (act ? el('p', { class: 'row__note', text: act.label }) : null)));
    }
    const waiting = ap.review.length + ap.parked.length;
    if (waiting) groups.push(el('p', { class: 'row__note', text: `${plural(ap.review.length, 'result')} to review and ${ap.parked.length} parked: see Needs you.` }));
    if (ap.events?.length) {
        groups.push(el('section', { class: 'autopilot__group autopilot__group--wide' }, el('h3', { class: 'card__sub', text: `Decisions (${ap.events.length})` }),
            el('ol', { class: 'decisions', tabindex: 0, 'aria-label': 'The autopilot\'s latest decisions, newest first' },
                ...more('ap:events', ap.events.map((e) => el('li', {},
                    el('time', { datetime: new Date(e.at * 1000).toISOString(), title: utcTitle(new Date(e.at * 1000).toISOString()), text: relative(new Date(e.at * 1000).toISOString()) }), ' ', e.text)), 4))));
    }
    $('autopilot').replaceChildren(...(groups.length ? groups : [empty('Backlog empty — add tasks',
        'tutti backlog add <repo> "<title>" --check "<command>", or tutti backlog seed <repo>.')]));
}

async function switchAutopilot(body, failText) {
    try {
        const res = await fetch('/api/autopilot', {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN }, body: JSON.stringify(body),
        });
        const answer = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
        await refresh();
    } catch (err) {
        showBanner(el('strong', { text: `${failText} ` }), err.message);
    }
}

async function toggleAutopilot() {
    const on = $('autopilot-toggle').getAttribute('aria-pressed') !== 'true';
    await switchAutopilot({ enabled: on }, 'The autopilot was not switched.');
}

// ---- scouts: helpers that answer on a GitHub issue (Grok Bot), with their weekly meter and the review trial

const REQ_TONE = { queued: 'idle', waiting: 'accent', answered: 'ok', failed: 'bad', timeout: 'warn' };

function scoutMeter(m) {
    if (!m || m.used === null || m.used === undefined) return 'no meter reading yet: read Grok Bot on the spending page with the reading helper';
    return `${tuttiPct(m.used)} of the week used · ${m.verdict}${m.reset ? ` · resets ${when(m.reset)}` : ''}`;
}

function scoutRequest(r) {
    const head = `${r.kind}${r.watch ? ` ${r.watch}` : ''} · ${r.task}`;
    const note = r.answer || r.why || (r.url ? `answer goes to ${r.url}` : 'not sent yet');
    return el('li', { class: 'row' },
        el('div', { class: 'row__name', title: r.task }, head.length > 110 ? `${head.slice(0, 110)}…` : head,
            el('span', { class: 'row__kind', text: `${r.id} · ${r.at ? relative(r.at) : ''}` })),
        chip(r.status, REQ_TONE[r.status] || 'idle'),
        el('p', { class: 'row__note', text: note.length > 400 ? `${note.slice(0, 400)}…` : note }),
        r.url && r.url.startsWith('https://') ? el('a', { href: r.url, target: '_blank', rel: 'noopener', text: 'Open on GitHub' }) : null);
}

function scoutBlock(sc) {
    const state = sc.ready ? chip(sc.auto ? 'ready · rounds on' : 'ready', 'ok') : chip(`not set up: run tutti scout setup grok in a terminal (${sc.missing.join(', ')} missing)`, 'bad');
    const watches = sc.watches.map((w) => el('li', { class: 'row' },
        el('div', { class: 'row__name', text: `${w.name} · every ${w.every}` }),
        el('p', { class: 'row__note', text: `last ${w.last ? relative(w.last) : 'never'} · next ${w.next ? when(w.next) : 'at the next round'}` })));
    return el('div', {},
        el('h3', { class: 'card__sub', text: `${sc.name} · ${sc.kind}` }), state,
        el('p', { class: 'row__note', text: `${scoutMeter(sc.meter)} · ${sc.waiting} waiting, ${sc.queued} queued · plan: ${sc.plan}` }),
        sc.week ? el('p', { class: 'row__note', 'data-week': sc.name, text: `Week: ${sc.week.text}` }) : '',
        sc.meter?.usage_url && (sc.meter.stale || sc.meter.used === null || sc.meter.used === undefined)
            ? el('a', { class: 'btn btn--tonal', href: sc.meter.usage_url, target: '_blank', rel: 'noopener noreferrer', text: 'Add a reading',
                title: `Opens ${sc.meter.usage_url}: click the reading helper there (it reads Grok Bot with Cursor)` }) : null,
        el('p', { class: 'row__note', text: `Skills: ${sc.skills.join(', ')} · answers in ${sc.repo}` }),
        watches.length ? el('ul', { class: 'list', 'aria-label': `${sc.name} watches` }, ...watches) : null,
        sc.recent.length ? el('ul', { class: 'list', 'aria-label': `${sc.name} recent questions` }, ...sc.recent.map(scoutRequest)) : null);
}

function trialBlock(t) {
    const rows = Object.entries(t.reviewers).map(([who, r]) => el('li', { class: 'row' },
        el('div', { class: 'row__name', text: who }),
        el('p', { class: 'row__note', text: `${r.raised} raised · ${r.real} real · ${r.noise} noise · ${r.open} to mark` })));
    const verdict = t.verdict ? t.verdict.why : `${t.closed} of ${t.limit} pull requests done; the verdict comes after the last one`;
    return el('div', {},
        el('h3', { class: 'card__sub', text: `Review trial: ${t.scout} (advisory, never blocking)` }),
        el('p', { class: 'row__note', text: `${verdict} · ${t.unique_real} real finding(s) only ${t.scout} raised` }),
        rows.length ? el('ul', { class: 'list' }, ...rows) : null);
}

function renderScouts(s) {
    const sc = s.scouts;
    if (!sc) return;
    $('scouts-meta').textContent = sc.configured ? `${sc.items.filter((i) => i.ready).length} of ${sc.items.length} ready` : 'none configured';
    const parts = sc.items.map(scoutBlock).concat((sc.trial || []).map(trialBlock));
    if (sc.error) parts.unshift(el('p', { class: 'row__note', text: `The last scouts round failed: ${sc.error}` }));
    if (sc.setup.length) {
        parts.push(el('details', { keep: 'scouts:setup', open: !sc.configured },
            el('summary', { text: 'Set up Grok Bot as a scout (owner, once)' }),
            el('ol', { class: 'list' }, ...sc.setup.map((step) => el('li', { text: step })))));
    }
    if (!sc.configured) parts.unshift(empty('No scouts yet', 'Run tutti scout setup grok in a terminal: it asks for what it needs and tests it.'));
    $('scouts').replaceChildren(...parts);
}

// ---- jobs

const LAYER = { exact: 'exact', shape: 'known phases', estimate: 'estimated from earlier runs', agent: 'agent estimate', elapsed: 'elapsed, no estimate yet' };

function compact(n) {
    if (n === null || n === undefined) return '?';
    return n >= 1e4 ? tuttiCompact(n) : tuttiNum(n, { digits: 0 });
}

// A finished job: its result, exit code (when not 0) and how long it took.
function finishedText(f) {
    const parts = [f.result || 'finished'];
    if (f.rc !== undefined && f.rc !== 0) parts.push(`exit ${f.rc}`);
    if (f.seconds !== undefined) parts.push(`took ${duration(f.seconds)}`);
    return parts.join(' · ');
}

// Running jobs: a bar for layers 1 to 3 (and an agent's rough estimate), labelled with where the number comes from; an
// elapsed time with no bar when nothing can say how far it is. Finished jobs: no bar, their result and duration.
function progressBar(j) {
    const p = j.progress;
    if (!p) return el('span', { class: 'job__progress', 'data-layer': 'finished', text: finishedText(j.facts || {}) });
    if (!p.determinate) return el('span', { class: 'job__progress', 'data-layer': p.layer, title: p.label, text: duration(p.elapsed_seconds) });
    const bar = el('span', { class: 'job__bar', role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': p.pct,
        'aria-valuetext': p.label, 'aria-label': `${j.title}: ${p.label}`, 'data-late': p.late ? 'true' : null });
    const fill = el('span');
    fill.style.width = tuttiCssPct(p.pct);
    bar.append(fill);
    const rough = p.layer === 'estimate' || (p.layer === 'agent' && p.total === undefined);
    return el('span', { class: 'job__progress', 'data-layer': p.layer, title: p.label }, bar, el('span', { text: (rough ? '~' : '') + tuttiPct(p.pct) }));
}

function layerText(p) {
    const parts = [LAYER[p.layer] || p.layer];
    if (p.eta_seconds) parts.push(`~${duration(p.eta_seconds)} left`);
    if (p.late) parts.push('longer than usual');
    return parts.join(' · ');
}

function tokenText(t) {
    const calls = t.calls ? ` · ${t.calls} tool calls` : '';
    return `tokens ${compact(t.in)} in · ${compact(t.out)} out` + calls;
}

function accountText(name) {
    const used = ui.accounts.get(name)?.used;
    return `account ${name}` + (used === null || used === undefined ? '' : ` (${tuttiPct(used)} used)`);
}

const PASSFAIL = (label, c) => `${label} ${c.passed} passed · ${c.failed} failed`;

// What a job did or has done so far: tokens, credits, the account it spends, tests, files changed, bytes copied, the step.
function factList(j) {
    const f = j.facts || {};
    const rows = [
        [j.progress, () => layerText(j.progress)],
        [f.step, () => `step: ${f.step}`],
        [f.tokens, () => tokenText(f.tokens)],
        [f.context_pct !== undefined && f.context_pct !== null, () => `context ${tuttiPct(f.context_pct)}`],
        [f.credits, () => `${f.credits} AI credits`],
        [f.worker && !f.account, () => `worker ${f.worker}`],
        [f.account, () => accountText(f.account)],
        [f.tests, () => PASSFAIL('tests', f.tests)],
        [f.checks, () => PASSFAIL('checks', f.checks)],
        [f.files, () => `${f.files.files} file(s) +${f.files.plus} −${f.files.minus}`],
        [f.copied, () => `copied ${f.copied.done}/${f.copied.total} ${f.copied.unit}`],
    ];
    return rows.filter(([on]) => on).map(([, text]) => text());
}

function job(j) {
    const d = el('details', { class: 'job', 'data-status': j.status, 'data-id': j.id, open: ui.open.has(j.id) });
    d.addEventListener('toggle', () => {
        if (d.open) {
            ui.open.add(j.id);
            saveUi();
            const pre = d.querySelector('pre');
            if (pre) pre.scrollTop = pre.scrollHeight;
        } else {
            ui.open.delete(j.id);
            saveUi();
        }
    });
    let idle = j.updated ? `updated ${relative(j.updated)}` : '';
    if (j.status === 'stalled') idle = `no output for ${duration(j.idle_seconds)}`;
    const meta = j.pid ? `${j.kind} · pid ${j.pid}` : j.kind;
    const facts = factList(j);
    d.append(el('summary', {},
        chip(j.status === 'failed' && j.rc !== null ? `failed · ${j.rc}` : j.status, JOB_TONE[j.status]),
        el('span', { class: 'job__title' }, j.title, el('small', { text: meta })),
        progressBar(j),
        el('span', { class: 'job__when', text: idle }),
        ...(facts.length ? [el('span', { class: 'job__facts', text: facts.join(' · ') })] : [])));
    const body = el('div', { class: 'job__body' });
    if (j.status === 'stalled') body.append(el('p', { text: 'Its log has not changed for a while. Read the end of the log: stop or restart the job rather than wait on it again.' }));
    if (j.status === 'ended') body.append(el('p', { text: 'Ended; the result was not recorded (a run from before Tutti 0.1.4).' }));
    if (j.handoff) body.append(el('p', { class: 'job__handoff', text: `Stopped from the dashboard: ${j.handoff}` }));
    if (j.progress) body.append(el('p', { class: 'job__path', text: `progress: ${j.progress.label}` }));
    if (j.started) body.append(el('p', { class: 'job__path', text: `started ${when(j.started)}` }));
    if (j.facts?.report) body.append(el('p', { class: 'job__path', text: `report ${j.facts.report}` }));
    if (j.log) body.append(el('p', { class: 'job__path', text: j.log }));
    body.append(j.tail.length ? el('pre', { class: 'log', tabindex: 0, 'aria-label': `End of the log of ${j.title}`, text: j.tail.join('\n') })
        : el('p', { class: 'job__path', text: 'No log output.' }));
    d.append(body);
    return d;
}

function renderProjectPick(s) {
    const pick = $('job-project');
    if (document.activeElement === pick) return;
    const names = (s.projects?.items || []).map((p) => p.name);
    if (ui.project && ui.project !== '-' && !names.includes(ui.project)) names.push(ui.project);
    pick.replaceChildren(el('option', { value: '', text: 'All projects', selected: !ui.project }),
        ...names.map((n) => el('option', { value: n, text: n, selected: ui.project === n })),
        el('option', { value: '-', text: 'No project', selected: ui.project === '-' }));
}

// Fills one job list and keeps the reader's place: the focused job (or its log) and where each open log was scrolled.
function fillJobs(box, jobs, none, key, limit) {
    if (!jobs.length) {
        box.replaceChildren(none);
        return;
    }
    const active = document.activeElement;
    const focused = box.contains(active) ? active.closest('.job')?.dataset.id : null;
    const focusedLog = active?.tagName === 'PRE';
    // Each open log keeps where the reader was; it follows new output only when it was already at the bottom.
    const where = new Map([...box.querySelectorAll('details[open] pre')].map((pre) => [pre.closest('.job').dataset.id,
        { top: pre.scrollTop, bottom: pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4 }]));
    box.replaceChildren(...more(key, jobs.map(job), limit, 'div'));
    for (const pre of box.querySelectorAll('details[open] pre')) {
        const w = where.get(pre.closest('.job').dataset.id);
        pre.scrollTop = !w || w.bottom ? pre.scrollHeight : w.top;
    }
    if (focused) box.querySelector(`[data-id="${CSS.escape(focused)}"]`)?.querySelector(focusedLog ? 'pre' : ':scope > summary')?.focus({ preventScroll: true });
}

const finishedAs = (j, f) => f === 'all' || j.status === f || (f === 'done' && j.status === 'ended');

// Running jobs and Queued use the height of the column beside them: rows that fit show, "Show more" only for the rest.
// Measured after each render and whenever either column changes size (ResizeObserver); a phone (stacked columns) keeps 8.
function measureFit() {
    const side = $('side-stack'), stack = $('jobs-stack');
    if (!side || !stack) return;
    const lists = [$('running'), $('queued')];
    const rowOf = (box) => box.querySelector('.job, .row')?.getBoundingClientRect().height || 72;
    const listsH = lists.reduce((h, box) => h + (box?.getBoundingClientRect().height || 0), 0);
    const fit = tuttiFit({ column: side.getBoundingClientRect().height, others: stack.getBoundingClientRect().height - listsH,
        rowRunning: rowOf(lists[0]), rowQueued: rowOf(lists[1]), running: ui.counts?.running || 0, queued: ui.counts?.queued || 0,
        phone: !window.matchMedia('(min-width: 960px)').matches });
    if (fit.running !== ui.fit?.running || fit.queued !== ui.fit?.queued) {
        ui.fit = fit;
        render();
    }
}

function watchFit() {
    if (ui.fitWatch || typeof ResizeObserver !== 'function') return;
    ui.fitWatch = new ResizeObserver(() => requestAnimationFrame(measureFit));
    for (const id of ['side-stack', 'jobs-stack']) if ($(id)) ui.fitWatch.observe($(id));
}

function renderJobs(s) {
    ui.accounts = new Map((s.usage?.accounts || []).map((a) => [a.name, a]));
    renderProjectPick(s);
    const items = s.jobs.items.filter((j) => !ui.project || (ui.project === '-' ? !j.project : j.project === ui.project));
    const running = items.filter((j) => LIVE.has(j.status));
    const finished = items.filter((j) => !LIVE.has(j.status));
    fillJobs($('running'), running, empty('Nothing running', codeText('Runs from ', 'tutti quiet', ', ship checks, transfers and the autopilot show here with their progress.')),
        'running', ui.fit?.running ?? 8);
    ui.counts = { running: running.length, queued: s.queue?.queued || 0 };
    watchFit();
    $('jobs-meta').textContent = ui.project ? `of ${ui.project === '-' ? 'no project' : ui.project} (choose in Running jobs)` : '';
    const filterFocus = document.activeElement?.closest?.('#job-filter') ? document.activeElement.dataset.f : null;
    $('job-filter-buttons').replaceChildren(...FILTERS.map(([f, label], i) => el('button', {
        type: 'button', 'data-f': f, 'aria-pressed': String(ui.filter === f), title: `Show ${label.toLowerCase()} finished jobs (${i + 1})`,
        text: `${label} ${finished.filter((j) => finishedAs(j, f)).length}`,
        onclick: () => { ui.filter = f; render(); },
    })));
    if (filterFocus) $('job-filter-buttons').querySelector(`[data-f="${filterFocus}"]`)?.focus();
    const none = finished.length ? empty(`No ${ui.filter} jobs`, 'Choose All to see every finished job.')
        : empty('No finished jobs yet', 'Each run shows here when it ends, with its result and the end of its log.');
    fillJobs($('jobs'), finished.filter((j) => finishedAs(j, ui.filter)), none, 'history', 10);
}

// ---- what needs the owner: tuttiAttention (attention.js) gathers it, most urgent first

const ATTENTION_LABEL = { 'needs-person': 'Needs a person', panic: 'Panic', stalled: 'Stalled', offline: 'Offline', down: 'Down',
    reading: 'Reading', review: 'To review', outside: 'Outside Tutti' };

function attentionItem(it) {
    const open = it.job ? el('button', { class: 'btn btn--text view-ok', type: 'button', text: 'Open its log', onclick: () => openJob(it.job) }) : null;
    return el('li', { class: 'row row--attention', 'data-tone': it.tone },
        el('div', { class: 'row__name', title: it.title }, it.title.length > 140 ? `${it.title.slice(0, 140)}…` : it.title),
        chip(ATTENTION_LABEL[it.kind] || it.kind, it.tone),
        el('p', { class: 'row__note', text: it.note }), open, it.task ? attemptHistory(it.task) : null);
}

function openJob(id) {
    ui.open.add(id);
    ui.project = '';            // the job may belong to a project the filter hides,
    ui.expanded.add('running'); // or sit past the fold of the running list
    render();
    const box = document.querySelector(`.job[data-id="${CSS.escape(id)}"]`);
    box?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    box?.querySelector(':scope > summary')?.focus({ preventScroll: true });
}

function renderAttention(s) {
    const items = tuttiAttention(s);
    const urgent = items.filter((i) => i.tone !== 'ok').length;
    $('attention-meta').textContent = items.length ? `${urgent} to act on · ${items.length - urgent} to review` : 'All clear';
    $('attention').replaceChildren(...(items.length ? more('attention', items.map(attentionItem), 6)
        : [el('li', {}, empty('Nothing needs you', 'No task waits for a person, no result to review, no warning.'))]));
}

// ---- the loop

function render() {
    const s = ui.last;
    if (!s) return;
    saveUi();   // the filters, the project, Show more and the open jobs, as this render shows them
    const now = Date.now();
    $('config-path').textContent = s.config;
    $('config-path').title = s.config;
    $('foot-version').textContent = `Tutti ${s.version}`;
    renderTiles(s);
    ui.autopilot = s.autopilot;   // the accounts' room lines say what the autopilot does with the room
    ui.scouts = s.scouts;         // a scout's account card shows its scout facts, not task room
    renderUsage(s, now);
    if (typeof renderLocalModels === 'function') renderLocalModels(s);   // energy.js: the Local models category
    if (s.household_use) $('household-use').textContent = s.household_use.headline;   // docs/FLEET.md, Household use
    renderAttention(s);
    renderControls(s);   // controls.js: devices, pause and resume, opt-in switches
    renderJobs(s);
    if (typeof renderQueue === 'function') renderQueue(s);   // queue.js: the Queued list beside Running jobs
    renderMachines(s);
    renderWorkBrowser(s);
    renderPresence(s);   // controls.js: work waiting on away or asleep targets, the host's warning
    renderAutopilot(s);
    renderScouts(s);
    renderMain(s);
    renderWorkers(s);
    renderVersions(s);
    renderProjects(s);
    if (typeof renderSetup === 'function') renderSetup(s);   // setup.js: the Set up card
    if (typeof renderWhere === 'function') renderWhere(s);   // where.js: Where Tutti runs
    renderTelemetry(s);   // telemetry.js: Insights, the Self-improvement lane, the sending switch
    renderHousehold(s);   // household.js: the household card (shown in the Household view)
    if (s.probe_error) showBanner(s.probe_error);
    else if (ui.notice) showBanner(ui.notice);
    else if (s.releases_stuck) showBanner(s.releases_stuck);   // merged versions no GitHub release publishes
    else if (!ui.failing) $('banner').hidden = true;
}

function showBanner(...parts) {
    const b = $('banner');
    b.replaceChildren(...parts);
    b.hidden = false;
}

function live() {
    const l = $('live');
    if (ui.failing) {
        l.dataset.state = 'down';
        $('live-text').textContent = 'Service not answering';
    } else if (ui.fetchedAt) {
        l.dataset.state = 'ok';
        const s = Math.round((Date.now() - ui.fetchedAt) / 1000);
        $('live-text').textContent = s < 2 ? 'Live · just now' : `Live · ${s} s ago`;
    }
}

async function refresh(how) {
    clearTimeout(ui.timer);
    // Only the newest request may publish its answer and schedule the next poll: a Refresh click during a slow
    // request must not start a second polling loop or let the older answer overwrite the newer one.
    const seq = ++ui.seq;
    try {
        // no-cache: the browser asks with the ETag it kept; an unchanged answer comes back as a 304 (no body sent)
        // and is neither parsed nor rendered again
        const res = await fetch('/api/state', { cache: 'no-cache' });
        // a bare 304 keeps the last answer (the browser normally turns its own revalidation into a 200 from its cache)
        if (!res.ok && res.status !== 304) throw new Error(`HTTP ${res.status}`);
        const etag = res.headers.get('ETag');
        // only a timed poll skips an unchanged answer: after a save or a click the page renders its own changes too
        const same = how === 'poll' && (res.status === 304 ? Boolean(ui.last) : etag && etag === ui.etag && ui.last && !ui.failing);
        if (res.status === 304 && !ui.last) throw new Error('HTTP 304 with no earlier answer');
        const data = same || res.status === 304 ? ui.last : await res.json();
        if (seq !== ui.seq) return;
        ui.fetchedAt = Date.now();
        if (!same) {
            ui.last = data;
            ui.etag = etag;
            ui.failing = false;
            render();
        }
        void selfUpdate(data.version);
    } catch {
        if (seq !== ui.seq) return;
        ui.failing = true;
        showBanner(el('strong', { text: 'The Tutti service on this computer is not answering. ' }),
            codeText('Start it again with ', 'tutti dashboard', ' and this page reconnects by itself.'),
            ui.last ? ' The data below is from the last good answer.' : '');
    }
    live();
    const every = (ui.last?.refresh_seconds || 10) * 1000;
    if (!document.hidden) ui.timer = setTimeout(() => refresh('poll'), ui.failing ? Math.min(every, 5000) : every);
}

function accentNote() {
    const shared = window.CSS && CSS.supports('color', 'AccentColor');
    $('foot-accent').textContent = shared ? 'Accent colour: from your system.'
        : "Accent colour: Tutti's indigo (this browser does not share the system accent colour).";
}

async function refreshReadings() {
    const b = $('refresh-readings');
    b.disabled = true;
    b.textContent = 'Reading…';
    try {
        const res = await fetch('/api/refresh', {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN }, body: '{}',
        });
        const answer = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
        await refresh();
    } catch (e) {
        showBanner(el('strong', { text: 'Usage was not read. ' }), e.message);
    } finally {
        b.disabled = false;
        b.textContent = 'Refresh readings';
    }
}

// ---- updating itself: a restarted dashboard on a new release is picked up with no click

function marker() {
    try {
        return { value: sessionStorage.getItem(RELOADED_FOR), ok: true };
    } catch {
        return { value: null, ok: false };   // storage blocked: the page cannot remember a reload, so it never reloads by itself
    }
}

function newWorker(reg) {
    const waiting = reg.waiting || reg.installing;
    if (!waiting) return Promise.resolve();
    return new Promise((resolve) => {
        const timer = setTimeout(resolve, 5000);
        navigator.serviceWorker.addEventListener('controllerchange', () => { clearTimeout(timer); resolve(); }, { once: true });
        waiting.postMessage({ type: 'skip-waiting' });
    });
}

function busyEditing() {
    return ui.editing.size > 0 || Boolean(ui.formPending);   // a reading being typed or saved is never thrown away
}

function writeMarker(version) {
    try {
        sessionStorage.setItem(RELOADED_FOR, version);
        return true;
    } catch {
        return false;   // full or blocked storage: the reload could not be remembered, so it does not happen
    }
}

function manualReload(version) {
    ui.notice = `Tutti ${version} is running but this page is still ${PAGE_VERSION}: reload it with Ctrl+Shift+R.`;
}

async function selfUpdate(version) {
    const m = marker();
    const what = tuttiUpdateDecision(PAGE_VERSION, version, m.value, m.ok);
    if (what === 'none' || ui.updating) return;
    if (what === 'stuck') {
        manualReload(version);
        return;
    }
    if (busyEditing()) return;   // the next poll tries again
    ui.updating = true;
    try {
        const reg = 'serviceWorker' in navigator ? await navigator.serviceWorker.getRegistration().catch(() => null) : null;
        if (reg) {
            await reg.update().catch(() => {});
            await newWorker(reg);
        }
        if (busyEditing()) return;   // an edit started while the worker updated: after it is saved or closed
        if (!writeMarker(version)) {
            manualReload(version);
            return;
        }
        location.reload();
    } finally {
        ui.updating = false;
    }
}

function keys(e) {
    if (e.target.closest('input, textarea, select') || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === 'r' || e.key === 'R') void refresh();
    const i = Number(e.key);
    if (i >= 1 && i <= FILTERS.length) {
        ui.filter = FILTERS[i - 1][0];
        render();
    }
}

function install() {
    let prompt = null;
    window.addEventListener('beforeinstallprompt', (e) => {
        e.preventDefault();
        prompt = e;
        $('install').hidden = false;
    });
    $('install').addEventListener('click', async () => {
        if (!prompt) return;
        prompt.prompt();
        await prompt.userChoice;
        prompt = null;
        $('install').hidden = true;
    });
    window.addEventListener('appinstalled', () => { $('install').hidden = true; });
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});
}

// ---- the reading helper's bookmarklet opens /#reading=<json>: saved when it carries this install's key (the server checks it),
// else (a page that opened the dashboard with a made-up reading, or nothing was found) shown in the form for the owner to save.
async function postHelperReading(r, key) {
    const body = { account: r.account, used: r.used, key, note: String(r.note || '').slice(0, 200) };
    if (r.resets) body.resets = r.resets;
    const res = await fetch('/api/reading', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN }, body: JSON.stringify(body),
    }).catch(() => null);
    const answer = res ? await res.json().catch(() => ({})) : {};
    return res?.ok ? '' : answer.error || 'the dashboard did not answer';
}

function openHelperForm(account, used, resets) {
    ui.editing.set(account, { used: Math.round(typeof used === 'number' ? used : 0), resets: localInput(resets), note: '', msg: 'From the helper: check and save', tone: '' });
    ui.formPending = true;   // the next render draws the form even though a form is "open"
}

async function fromHelper() {
    const h = location.hash;
    if (!h.startsWith('#reading=')) return;
    history.replaceState(null, '', `${location.pathname}#dashboard`);   // the reading helper's result shows on the dashboard
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    let r;
    try { r = JSON.parse(decodeURIComponent(h.slice('#reading='.length))); } catch { return; }
    if (!r || typeof r.account !== 'string') return;
    // one bookmarklet click may carry several meters of one page: {key, account, readings: [{account, used, resets, note}]}
    const list = Array.isArray(r.readings) ? r.readings : [r];
    const valid = list.filter((x) => x && typeof x.account === 'string' && typeof x.used === 'number');
    if (!valid.length || !r.key) {
        openHelperForm(r.account, valid[0]?.used, valid[0]?.resets);
        if (r.key) banner('The helper found no reading on that page: type it in the form.');
        return;
    }
    const saved = [];
    for (const x of valid) {
        const error = await postHelperReading(x, r.key);
        if (error) {
            banner(`The helper's reading for ${x.account} was not saved: ${error}. Check it in the form.`);
            openHelperForm(x.account, x.used, x.resets);
            return;
        }
        saved.push(`${x.account} ${tuttiPct(x.used)}`);
    }
    banner(`Saved, read by the helper: ${saved.join(', ')}.${await notRead(valid, r.page)}`);
}

// An account on the same usage page that the click did not carry: the button was dragged before that account was configured.
async function notRead(valid, page) {
    const where = page || (String(valid[0].note || '').split(' ')[2] || '');
    const res = await fetch('/api/state', { cache: 'no-store' }).catch(() => null);
    const snap = res?.ok ? await res.json().catch(() => null) : null;
    const got = new Set(valid.map((x) => x.account));
    const left = (snap?.usage?.accounts || []).filter((a) => a.usage_url && where && a.usage_url.includes(where) && !got.has(a.name));
    return left.length ? ` Not read: ${left.map((a) => a.name).join(', ')}, because this helper button is older than the config; ` +
        'drag it from the helper page again (Usage, helper) and click it once more on the page.' : '';
}

function banner(text) {
    ui.notice = text;   // kept across polls until the page is reloaded
    showBanner(text);
}

document.addEventListener('DOMContentLoaded', () => {
    loadUi();   // the folds, filters and open jobs this tab had before a reload
    void fromHelper().then(() => refresh());
    window.addEventListener('hashchange', () => { void fromHelper().then(() => refresh()); });   // the dashboard tab was already open
    $('refresh').addEventListener('click', refresh);
    $('refresh-readings').addEventListener('click', refreshReadings);
    $('main-form').addEventListener('submit', switchMain);
    $('autopilot-toggle').addEventListener('click', toggleAutopilot);
    $('update-now').addEventListener('click', updateNow);
    $('job-project').addEventListener('change', (e) => { ui.project = e.target.value; render(); });
    document.addEventListener('keydown', keys);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) void refresh(); });
    setInterval(live, 1000);
    accentNote();
    install();
    void refresh();
});
