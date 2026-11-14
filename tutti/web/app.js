// Tutti dashboard: polls /api/state and renders it with DOM nodes and textContent only (no HTML from data).
'use strict';

const $ = (id) => document.getElementById(id);
const FILTERS = [['all', 'All'], ['running', 'Running'], ['stalled', 'Stalled'], ['failed', 'Failed'], ['done', 'Done']];
const VERDICT_TONE = { 'on pace': 'ok', ahead: 'warn', behind: 'warn', late: 'bad', spent: 'bad', 'no reading': 'idle', unknown: 'idle' };
const MACHINE_TONE = { free: 'ok', loaded: 'warn', busy: 'accent', paused: 'warn', offline: 'bad' };
const JOB_TONE = { running: 'accent', stalled: 'warn', failed: 'bad', done: 'ok', ended: 'idle' };
const storedOpen = new Set();   // machines whose stored-files list is open: kept across re-renders
const ui = { filter: 'all', project: '', open: new Set(), last: null, fetchedAt: 0, timer: 0, failing: false, seq: 0, editing: new Map() };
const TOKEN = document.querySelector('meta[name="tutti-token"]')?.content || '';
const RESET_FROM = { reading: 'from the latest reading', estimate: 'an estimate from the config: add a reading to set it', config: 'from the config' };

function el(tag, props, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
        if (v === undefined || v === null || v === false) continue;
        if (k === 'text') node.textContent = v;
        else if (k === 'class') node.className = v;
        else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
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
    const used = Math.min(100, a.used ?? 0);
    let tone;
    if (a.panic) tone = 'bad';
    else if (a.verdict === 'ahead') tone = 'warn';
    const m = el('div', {
        class: 'meter', 'data-tone': tone, role: 'meter', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': used,
        'aria-label': `${a.name}: ${used} % used`,
    });
    const fill = el('div', { class: 'meter__fill' });
    fill.style.width = `${used}%`;
    m.append(fill);
    if (a.expected !== null) {
        const mark = el('div', { class: 'meter__mark', title: `By now: ${a.expected} %` });
        mark.style.left = `${Math.min(100, a.expected)}%`;
        m.append(mark);
    }
    const p = el('div', { class: 'meter__mark meter__mark--panic', title: `Panic at ${a.panic_at} %` });
    p.style.left = `${a.panic_at}%`;
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
    const pct = el('output', { class: 'reading__pct', for: id('used'), text: `${f.used} %` });
    const form = el('form', { class: 'reading', 'aria-label': `New reading for ${a.name}` },
        el('label', { for: id('used') }, el('span', {}, 'Used now: ', pct),
            el('input', { id: id('used'), type: 'range', min: 0, max: 100, step: 1, value: f.used,
                oninput: (e) => { f.used = e.target.value; pct.textContent = `${f.used} %`; } })),
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
        ['Share', a.share < 1 ? `${Math.round(a.share * 100)} % of the quota is this session's` : null], ['Read at', a.source],
    ]);
}

function addButton(a) {
    return el('button', {
        class: 'btn btn--tonal account__add', type: 'button', 'data-add': a.name, text: 'Add a reading',
        title: a.usage_url ? `Opens ${a.usage_url} and a short form` : 'Opens a short form', onclick: () => openReading(a),
    });
}

function account(a, now) {
    const verdict = a.panic ? 'panic' : a.verdict;
    const card = el('article', { class: 'account', 'data-panic': String(a.panic), 'aria-label': `${a.name}: ${verdict}` },
        el('div', { class: 'account__top' }, el('h3', { class: 'account__name', text: a.name }),
            chip(verdict, a.panic ? 'bad' : VERDICT_TONE[a.verdict])));
    if (a.state === 'ok') {
        card.append(el('div', { class: 'account__big' }, el('span', { class: 'account__pct', text: `${a.used} %` }),
            el('span', { class: 'account__by', text: `used · ${a.expected} % by now on an even pace` })));
        card.append(meter(a));
        card.append(el('div', { class: 'legend' }, el('span', {}, el('i', { class: 'l-used' }), 'used'),
            el('span', {}, el('i'), 'pace line (by now)'), el('span', {}, el('i', { class: 'l-panic' }), `panic at ${a.panic_at} %`)));
    } else {
        card.append(el('p', { class: 'account__by', text: a.state === 'no reset' ? 'No reset time in the config yet.' : 'No reading in this window yet.' }));
    }
    if (a.reset) card.append(sparkline(a, now));
    card.append(el('p', { class: 'account__action', text: `Do: ${a.action}` }));
    if (a.room_tasks) card.append(el('p', { class: 'account__nudge', 'data-tone': 'ok', text: `Use it: ${a.room_tasks} bounded task(s) can go here now (tutti run auto "<task>" --repo <repo>)` }));
    if (a.note) card.append(el('p', { class: 'account__note', text: a.note }));
    if (a.credits_used !== undefined) card.append(el('p', { class: 'account__auto', text: `${a.credits_used} AI credits in this window from Tutti's runs` }));
    if (a.stale) card.append(el('p', { class: 'account__remind', 'data-tone': 'warn', role: 'note', text: `Reminder: ${a.remind}` }));
    card.append(accountFacts(a), autoLine(a) || '', ui.editing.has(a.name) ? readingForm(a) : addButton(a));
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
    $('usage').replaceChildren(...(u.accounts.length ? u.accounts.map((a) => account(a, now))
        : [empty('No accounts yet', codeText('Add an accounts: section to the config (see ', 'tutti.config.example.yaml', ').'))]));
}

// ---- machines

function pips(m) {
    return el('span', { class: 'pips', 'aria-label': m.free === null ? `${m.slots} slots, not reachable` : `${m.busy} of ${m.slots} slots busy` },
        Array.from({ length: m.slots }, (_, i) => el('span', { class: 'pip', 'data-busy': String(m.free !== null && i < m.busy), 'data-off': String(m.free === null) })));
}

function machine(m) {
    const stats = el('div', { class: 'row__stats' }, pips(m),
        el('span', { text: m.free === null ? `${m.slots} slots` : `${m.free} of ${m.slots} slots free` }));
    if (m.load !== null) {
        const bar = el('span');
        bar.style.width = `${Math.min(100, (m.load / m.cores) * 100)}%`;
        stats.append(el('span', { class: 'load' }, el('span', { class: 'load__bar', 'aria-hidden': 'true' }, bar), `load ${m.load} / ${m.cores} cores`));
    }
    if (m.temp_c !== null && m.temp_c !== undefined) {
        const hot = m.heat?.cap_c !== undefined && m.temp_c >= m.heat.cap_c;
        stats.append(el('span', { class: 'temp', 'data-tone': hot ? 'warn' : undefined, text: `${Math.round(m.temp_c)} °C` }));
    }
    if (m.rank) stats.append(el('span', { text: `#${m.rank} in routing` }));
    return el('li', { class: 'row' },
        el('div', { class: 'row__name' }, m.name, el('span', { class: 'row__kind', text: `${m.transport} · ${m.host}` })),
        chip(m.status, MACHINE_TONE[m.status]),
        stats,
        el('p', { class: 'row__note', text: m.status === 'paused' ? 'Yield flag is up: Tutti sends it nothing new until it is lowered.' : m.reason }),
        storedFiles(m.name, m.stored));
}

function bytes(n) {
    const units = [['GB', 1024 ** 3], ['MB', 1024 ** 2], ['KB', 1024]];
    const [unit, size] = units.find(([, s]) => n >= s) || ['B', 1];
    return `${size === 1 ? n : (n / size).toFixed(1)} ${unit}`;
}

// The files `tutti files send` keeps in the machine's cache: what, how big, last used, against the budget.
function storedFiles(name, st) {
    if (!st) return null;
    if (st.problem) return el('p', { class: 'row__note', text: `Stored files: ${st.problem}` });
    const head = `Stored files: ${bytes(st.bytes)} of ${bytes(st.budget_bytes)} in ${st.cache}`;
    if (!st.items.length) return el('p', { class: 'row__note', text: `${head}. None yet: tutti files send <path> --to <machine>` });
    const d = el('details', { class: 'stored', open: storedOpen.has(name), 'data-machine': name });
    d.addEventListener('toggle', () => (d.open ? storedOpen.add(name) : storedOpen.delete(name)));
    d.append(el('summary', { text: `${head} · ${st.items.length} item${st.items.length === 1 ? '' : 's'}` }),
        el('ul', { class: 'stored__list' }, ...st.items.map((it) => el('li', {},
            el('span', { class: 'stored__name', text: it.name }),
            el('span', { text: `${bytes(it.bytes)} · used ${relative(new Date(it.used_at * 1000).toISOString())}` })))));
    return d;
}

function renderMachines(s) {
    const ms = s.machines;
    $('machines-meta').textContent = ms.probed_at ? `Probed ${relative(ms.probed_at)}` : 'Probing…';
    // A re-render replaces the rows: the stored-files toggle that had focus gets it back (keyboard users keep their place).
    const focused = document.activeElement?.closest?.('.stored')?.dataset.machine;
    $('machines').replaceChildren(...(ms.items.length ? ms.items.map(machine)
        : [el('li', {}, ms.probed_at ? empty('No machines', 'Add machines: to the config to route heavy runs.') : empty('Probing machines…', 'ssh answers can take a few seconds.'))]));
    if (focused) $('machines').querySelector(`.stored[data-machine="${CSS.escape(focused)}"] > summary`)?.focus({ preventScroll: true });
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
}

// ---- projects

function machineLine(m) {
    const parts = [`${m.name}: ${m.processes} process${m.processes === 1 ? '' : 'es'}`];
    if (m.agents) parts.push(`${m.agents} agent${m.agents === 1 ? '' : 's'}`);
    if (m.programs?.length) parts.push(m.programs.slice(0, 5).join(', '));
    if (m.yield_flag) parts.push('its yield flag is up (the machine is this project\'s)');
    if (m.gpu) parts.push(`GPU ${m.gpu.util_pct} % · ${Math.round(m.gpu.mem_used_mib / 1024)}/${Math.round(m.gpu.mem_total_mib / 1024)} GB`);
    return parts.join(' · ');
}

function project(p) {
    const busy = p.running + p.machines.reduce((n, m) => n + m.processes, 0);
    const repos = p.repos.map((r) => `${r.machine}: ${r.path}${r.branch ? ` (${r.branch})` : ''}`);
    const counts = `${p.running} running job${p.running === 1 ? '' : 's'} · ${p.agents} agent${p.agents === 1 ? '' : 's'} · ${p.runs} run${p.runs === 1 ? '' : 's'}`
        + (p.conductor ? ` · conducted by ${p.conductor}` : '');
    return el('li', { class: 'row' },
        el('div', { class: 'row__name' }, p.name, el('span', { class: 'row__kind', text: counts })),
        chip(busy ? 'active' : 'idle', busy ? 'accent' : 'idle'),
        el('p', { class: 'row__note', text: p.last_activity ? `Last activity ${relative(p.last_activity)}` : 'No activity seen' }),
        ...repos.map((r) => el('p', { class: 'row__note', text: r })),
        ...p.machines.map((m) => el('p', { class: 'row__note', text: machineLine(m) })),
        el('div', { class: 'row__stats' }, el('button', {
            class: 'btn btn--tonal', type: 'button', text: 'Show its jobs',
            onclick: () => { ui.project = p.name; render(); $('jobs-h').scrollIntoView({ behavior: 'smooth' }); },
        })));
}

function renderProjects(s) {
    const ps = s.projects;
    if (!ps) return;
    const errors = Object.entries(ps.errors || {}).map(([m, e]) => `${m}: ${e}`);
    $('projects-meta').textContent = errors.length ? `Not looked at: ${errors.join('; ')}` : `${ps.items.length} seen on your machines`;
    $('projects').replaceChildren(...(ps.items.length ? ps.items.map(project)
        : [el('li', {}, empty('No projects seen yet', 'Jobs and processes inside a git repository show their project here.'))]));
}

// ---- the main agent

function mainRun(m) {
    const r = m.running || m.last;
    if (!r) return empty('No conductor run yet', 'Start one with tutti conduct "<goal>"; it shows here.');
    const turns = (r.agents || []).map((t) => `${t.agent}${t.stopped ? ` (stopped: ${t.stopped})` : ''}`).join(' → ');
    return el('div', { class: 'row' },
        el('div', { class: 'row__name' }, m.running ? 'Running' : 'Last run', el('span', { class: 'row__kind', text: r.goal })),
        chip(r.status, m.running ? 'accent' : (r.status === 'done' ? 'ok' : 'warn')),
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
            text: `${c.name} (${c.kind === 'ollama' ? 'local model' : 'cli'})${c.out ? ` · out: ${c.out}` : ''}`,
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

// ---- jobs

function progressBar(j) {
    const p = j.progress;
    if (!p) return el('span', { class: 'job__progress', title: 'Nothing in its log says how far it is', text: '—' });
    const bar = el('span', { class: 'job__bar', role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': p.pct,
        'aria-label': `${j.title}: ${p.pct} %` });
    const fill = el('span');
    fill.style.width = `${p.pct}%`;
    bar.append(fill);
    let label = p.total === 100 || p.source === 'finished' ? `${p.pct} %` : `${p.pct} % · ${p.done}/${p.total}`;
    if (p.unit) label = `${p.pct} % · ${p.done}/${p.total} ${p.unit}`;
    return el('span', { class: 'job__progress', title: p.source === 'reported' ? 'reported with tutti progress' : 'read from its log' },
        bar, el('span', { text: label }));
}

function job(j) {
    const d = el('details', { class: 'job', 'data-status': j.status, 'data-id': j.id, open: ui.open.has(j.id) });
    d.addEventListener('toggle', () => {
        if (d.open) {
            ui.open.add(j.id);
            const pre = d.querySelector('pre');
            if (pre) pre.scrollTop = pre.scrollHeight;
        } else {
            ui.open.delete(j.id);
        }
    });
    let idle = j.updated ? `updated ${relative(j.updated)}` : '';
    if (j.status === 'stalled') idle = `no output for ${duration(j.idle_seconds)}`;
    const meta = j.pid ? `${j.kind} · pid ${j.pid}` : j.kind;
    d.append(el('summary', {},
        chip(j.status === 'failed' && j.rc !== null ? `failed · ${j.rc}` : j.status, JOB_TONE[j.status]),
        el('span', { class: 'job__title' }, j.title, el('small', { text: meta })),
        progressBar(j),
        el('span', { class: 'job__when', text: idle })));
    const body = el('div', { class: 'job__body' });
    if (j.status === 'stalled') body.append(el('p', { text: 'Its log has not changed for a while. Read the end of the log: stop or restart the job rather than wait on it again.' }));
    if (j.status === 'ended') body.append(el('p', { text: 'Ended; the result was not recorded (a run from before Tutti 0.1.4).' }));
    if (j.started) body.append(el('p', { class: 'job__path', text: `started ${when(j.started)}` }));
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

function renderJobs(s) {
    renderProjectPick(s);
    const items = s.jobs.items.filter((j) => !ui.project || (ui.project === '-' ? !j.project : j.project === ui.project));
    const count = (f) => (f === 'all' ? items.length : items.filter((j) => j.status === f || (f === 'done' && j.status === 'ended')).length);
    const filterFocus = document.activeElement?.closest?.('#job-filter') ? document.activeElement.dataset.f : null;
    $('job-filter-buttons').replaceChildren(...FILTERS.map(([f, label], i) => el('button', {
        type: 'button', 'data-f': f, 'aria-pressed': String(ui.filter === f), title: `Show ${label.toLowerCase()} jobs (${i + 1})`, text: `${label} ${count(f)}`,
        onclick: () => { ui.filter = f; render(); },
    })));
    if (filterFocus) $('job-filter-buttons').querySelector(`[data-f="${filterFocus}"]`)?.focus();
    const shown = items.filter((j) => ui.filter === 'all' || j.status === ui.filter || (ui.filter === 'done' && j.status === 'ended'));
    if (shown.length) {
        const active = document.activeElement;
        const focused = active?.closest?.('.job')?.dataset.id;
        const focusedLog = active?.tagName === 'PRE';
        // Each open log keeps where the reader was; it follows new output only when it was already at the bottom.
        const where = new Map([...$('jobs').querySelectorAll('details[open] pre')].map((pre) => [pre.closest('.job').dataset.id,
            { top: pre.scrollTop, bottom: pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4 }]));
        $('jobs').replaceChildren(...shown.map(job));
        for (const pre of $('jobs').querySelectorAll('details[open] pre')) {
            const w = where.get(pre.closest('.job').dataset.id);
            pre.scrollTop = !w || w.bottom ? pre.scrollHeight : w.top;
        }
        if (focused) {
            const box = $('jobs').querySelector(`[data-id="${CSS.escape(focused)}"]`);
            box?.querySelector(focusedLog ? 'pre' : ':scope > summary')?.focus({ preventScroll: true });
        }
    } else if (items.length) {
        $('jobs').replaceChildren(empty(`No ${ui.filter} jobs`, 'Choose All to see every recent job.'));
    } else {
        $('jobs').replaceChildren(empty('No jobs yet', codeText('Runs from ', 'tutti quiet', ', ship checks, file transfers and tutti wait show up here with the end of their log.')));
    }
}

// ---- the loop

function render() {
    const s = ui.last;
    if (!s) return;
    const now = Date.now();
    $('config-path').textContent = s.config;
    $('config-path').title = s.config;
    $('foot-version').textContent = `Tutti ${s.version}`;
    renderTiles(s);
    renderProjects(s);
    renderMain(s);
    renderUsage(s, now);
    renderMachines(s);
    renderWorkers(s);
    renderJobs(s);
    if (s.probe_error) showBanner(s.probe_error);
    else if (ui.notice) showBanner(ui.notice);
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

async function refresh() {
    clearTimeout(ui.timer);
    // Only the newest request may publish its answer and schedule the next poll: a Refresh click during a slow
    // request must not start a second polling loop or let the older answer overwrite the newer one.
    const seq = ++ui.seq;
    try {
        const res = await fetch('/api/state', { cache: 'no-store' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (seq !== ui.seq) return;
        ui.last = data;
        ui.fetchedAt = Date.now();
        ui.failing = false;
        render();
    } catch {
        if (seq !== ui.seq) return;
        ui.failing = true;
        showBanner(el('strong', { text: 'The Tutti service on this computer is not answering. ' }),
            codeText('Start it again with ', 'tutti dashboard', ' and this page reconnects by itself.'),
            ui.last ? ' The data below is from the last good answer.' : '');
    }
    live();
    const every = (ui.last?.refresh_seconds || 10) * 1000;
    if (!document.hidden) ui.timer = setTimeout(refresh, ui.failing ? Math.min(every, 5000) : every);
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
    history.replaceState(null, '', location.pathname);
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
        saved.push(`${x.account} ${x.used} %`);
    }
    banner(`Saved, read by the helper: ${saved.join(', ')}.`);
}

function banner(text) {
    ui.notice = text;   // kept across polls until the page is reloaded
    showBanner(text);
}

document.addEventListener('DOMContentLoaded', () => {
    void fromHelper().then(() => refresh());
    window.addEventListener('hashchange', () => { void fromHelper().then(() => refresh()); });   // the dashboard tab was already open
    $('refresh').addEventListener('click', refresh);
    $('refresh-readings').addEventListener('click', refreshReadings);
    $('main-form').addEventListener('submit', switchMain);
    $('job-project').addEventListener('change', (e) => { ui.project = e.target.value; render(); });
    document.addEventListener('keydown', keys);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) void refresh(); });
    setInterval(live, 1000);
    accentNote();
    install();
    void refresh();
});
