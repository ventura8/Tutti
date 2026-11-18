// Tutti dashboard: Android devices, pause and resume, opt-in switches. Uses app.js's helpers (el, $, chip, relative,
// refresh, showBanner, TOKEN). Every switch is a POST to /api/control, saved in the state folder (never in the config file).
'use strict';

const DEVICE_TONE = { person: 'warn', ready: 'ok', busy: 'accent', waiting: 'warn', offline: 'bad', held: 'warn', call: 'warn' };
const DEVICE_CHIP = { held: 'paused', call: 'Paused: phone call', person: 'in use by a person — waiting' };

async function control(body, failText) {
    try {
        const res = await fetch('/api/control', {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN }, body: JSON.stringify(body),
        });
        const answer = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
        if (answer.handed?.length) ui.notice = `Handed over: ${answer.handed.map((h) => h.to).join('; ')}`;
        await refresh();
    } catch (err) {
        showBanner(el('strong', { text: `${failText} ` }), err.message);
    }
}

function deviceStats(d) {
    const parts = [];
    if (d.battery !== null && d.battery !== undefined && d.battery_present !== 0 && d.kind !== 'tv') parts.push(`battery ${tuttiPct(d.battery)}`);
    if (d.charging === 1) parts.push('charging');
    else if (d.charging === 0 && d.battery_present !== 0 && d.kind !== 'tv') parts.push('on battery');
    const temps = d.card?.temps || [];
    parts.push(...(temps.length ? temps : d.status === 'offline' ? [] : ['temperature not readable']));
    if (d.screen === 1) parts.push('screen on');
    else if (d.screen === 0) parts.push('screen off');
    if (d.wifi === 1) parts.push('Wi-Fi');
    else if (d.wifi === 0) parts.push('no Wi-Fi');
    return el('div', { class: 'row__stats' }, ...parts.map((t) => el('span', { text: t })));
}

// The card's one action (/api/state devices.items[].card.action): Resume, the idle watchdog's fix (Check again now...), else Pause.
function deviceAction(d) {
    const a = d.card?.action;
    if (a) return el('button', { class: 'btn btn--tonal', type: 'button', onclick: async () => { await fixIdle(a); } }, a.label);
    return el('button', { class: 'btn btn--tonal', type: 'button', title: 'It takes nothing new; what runs now finishes',
        onclick: async () => { await control({ action: 'pause', kind: 'device', name: d.name }, `device ${d.name} was not paused.`); } }, 'Pause');
}

// One card per device: one status (the same pill everywhere), why, what waits on it, its action, stats, hours and night mode.
function device(d, paused) {
    const c = d.card || { status: d.status, label: DEVICE_CHIP[d.status] || d.status, tone: DEVICE_TONE[d.status], why: d.reason };
    const why = paused ? pausedBy(paused) : c.why;
    return el('li', { class: 'row', 'data-device': d.name, 'data-status': c.status },
        el('div', { class: 'row__name' }, d.name, el('span', { class: 'row__kind', text: `adb ${d.serial} · on ${d.machine}` })),
        chip(c.label, c.tone),
        el('div', { class: 'row__stats' }, deviceAction(d)),
        deviceStats(d),
        presenceBits(d.presence),
        why ? el('p', { class: 'row__note device__why', text: why }) : null,
        c.waiting ? el('p', { class: 'row__note device__waiting', text: `Waiting for it: ${c.waiting}.` }) : null,
        c.moved ? el('p', { class: 'row__note device__moved', text: `While away: ${c.moved}.` }) : null,
        blockedHours(d));
}

// The device's recurring blocked hours (devices.<d>.hours), Do Not Disturb, Cast and night mode: one short sentence each.
function blockedHours(d) {
    const h = d.hours;
    const bits = [el('span', {}, h ? `Not used ${h.window}${h.days ? ` on ${h.days.join(', ')}` : ' every day'} (local time). ` : 'No blocked hours. ',
        el('a', { class: 'link', href: `#settings/devices.${d.name}.hours`, text: h ? 'Change' : 'Set blocked hours' }))];
    if (d.dnd_now || d.dnd_schedule) bits.push(el('span', { text: `Do Not Disturb: ${d.dnd_now ? 'on now' : 'off now'}${d.dnd_schedule ? `, usually ${d.dnd_schedule}` : ''}.` }));
    if ((d.roles || []).length) bits.push(el('span', { text: `Cast: ${d.roles.map((r) => r.replace('cast-', '')).join(' and ')}.` }));
    if (d.night) bits.push(el('span', {}, `Night mode: ${d.night.hours} (1 % brightness, volume 0). `, el('a', { class: 'link', href: '#settings/night.enabled', text: 'Change' })));
    return el('ul', { class: 'device__notes', 'aria-label': `${d.name}: hours and modes` }, ...bits.map((b) => el('li', { class: 'row__note' }, b)));
}

function pausedBy(p) {
    if (!p) return '';
    const at = p.at ? relative(new Date(p.at * 1000).toISOString()) : '';
    const note = p.note ? `: ${p.note}` : '';
    if (p.from) {   // a planned absence (tutti pause --from --until)
        const when = (t) => new Date(t * 1000).toLocaleString([], { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
        const end = p.until ? when(p.until) : 'it is seen again';
        const head = p.window === 'planned' ? 'Away (planned)' : 'Away now';
        return `${head}: from ${when(p.from)} until ${end}${note}. No new work starts on it from 30 min before.`;
    }
    return `Paused by ${p.by || 'the owner'} ${at}${note}`;
}

function pausable(kind, name, paused, state) {
    const label = `${kind} ${name}`;
    const buttons = paused
        ? [el('button', { class: 'btn btn--tonal', type: 'button', onclick: async () => { await control({ action: 'resume', kind, name }, `${label} was not resumed.`); } }, 'Resume')]
        : [el('button', { class: 'btn btn--tonal', type: 'button', title: 'It takes nothing new; what runs now finishes',
            onclick: async () => { await control({ action: 'pause', kind, name }, `${label} was not paused.`); } }, 'Pause'),
        el('button', { class: 'btn', type: 'button', title: 'Hands its running jobs over to the next machine, device or worker, then stops them',
            onclick: async () => {
                if (window.confirm(`Pause ${label} and hand its running jobs over now?`)) {
                    await control({ action: 'pause', kind, name, stop: true }, `${label} was not paused.`);
                }
            } }, 'Pause, stop now')];
    return el('li', { class: 'row' },
        el('div', { class: 'row__name' }, name, el('span', { class: 'row__kind', text: kind })),
        chip(paused ? (paused.window === 'planned' ? 'away later' : paused.from ? 'away' : 'paused') : state || 'on', paused ? 'warn' : 'ok'),
        el('div', { class: 'row__stats' }, ...buttons),
        paused ? el('p', { class: 'row__note', text: pausedBy(paused) }) : null);
}

function optIn(o) {
    const label = `${o.kind} ${o.name}`;
    return el('li', { class: 'row' },
        el('div', { class: 'row__name' }, o.name, el('span', { class: 'row__kind', text: `${o.kind} · ${o.why}` })),
        chip(o.on ? 'on' : 'off', o.on ? 'ok' : 'idle'),
        el('div', { class: 'row__stats' }, el('button', { class: 'btn btn--tonal', type: 'button', 'aria-pressed': String(o.on),
            onclick: async () => {
                // the autopilot's own switches (fill_room) are saved by /api/autopilot, the entries' by /api/control
                if (o.kind === 'autopilot') await switchAutopilot({ [o.name]: !o.on }, `${label} was not switched.`);
                else if (o.kind === 'dispatch') await postDispatch({ enabled: !o.on }, 'Keep busy was not switched.');
                else await control({ action: 'optin', kind: o.kind, name: o.name, on: !o.on }, `${label} was not switched.`);
            } },
        o.on ? 'Turn off' : 'Turn on')));
}

// A scout (Grok Bot) is listed with the online agents; it pauses through its config's auto: and its account, so no button.
function scoutRow(sc) {
    let state = 'not set up: tutti scout setup grok';
    if (sc.ready) state = sc.auto ? 'ready · rounds on' : 'ready';
    return el('li', { class: 'row' },
        el('div', { class: 'row__name' }, sc.name, el('span', { class: 'row__kind', text: `scout · ${sc.kind}` })),
        chip(state, sc.ready ? 'ok' : 'bad'));
}

// Keep busy (tutti/dispatch.py): POST /api/dispatch {enabled} switches it, {action: "now"} runs a round now.
async function postDispatch(body, failText) {
    try {
        const res = await fetch('/api/dispatch', {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN }, body: JSON.stringify(body),
        });
        const answer = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
        await refresh();
    } catch (err) {
        showBanner(el('strong', { text: `${failText} ` }), err.message);
    }
}

async function fixIdle(f) {
    const failText = `${f.label} did not work.`;
    if (f.action === 'dispatch_on') await postDispatch({ enabled: true }, failText);
    else if (f.action === 'dispatch_now') await postDispatch({ action: 'now' }, failText);
    else if (f.action === 'autopilot_on') await switchAutopilot({ enabled: true }, failText);
    else if (f.action === 'resume') await control({ action: 'resume', kind: f.kind, name: f.name }, failText);
    else if (f.action === 'optin') await control({ action: 'optin', kind: f.kind, name: f.name, on: true }, failText);
}

// The idle watchdog: a device, machine or worker idle past dispatch.idle_minutes while work waits for it, and why.
function idleWarning(w) {
    return el('li', { class: 'row row--attention', 'data-tone': w.state === 'blocked' ? 'bad' : 'warn' },
        el('div', { class: 'row__name' }, w.name, el('span', { class: 'row__kind', text: w.kind })),
        chip(w.state, w.state === 'blocked' ? 'bad' : 'warn'),
        el('div', { class: 'row__stats' }, w.fix
            ? el('button', { class: 'btn btn--tonal', type: 'button', onclick: async () => { await fixIdle(w.fix); } }, w.fix.label) : null),
        el('p', { class: 'row__note', text: w.text }));
}

// Before the first probe answers the lists come from the config's names, marked checking (never "none in the config").
function probedOr(items, configured, probed) {
    return probed || items.length ? items : (configured || []).map((x) => ({ ...x, status: 'checking', checking: true }));
}

function renderControls(s) {
    const probed = Boolean(s.machines?.probed_at);
    const devices = probedOr(s.devices?.items || [], s.devices?.configured, probed);
    // a device's warning and reroute line live on its own card; only machines' and workers' stay in this list
    const onCards = new Set(devices.map((d) => d.name));
    const moved = devices.map((d) => d.card?.moved).filter(Boolean);
    $('idle-warnings').replaceChildren(...(s.dispatch?.warnings || []).filter((w) => !(['android', 'tv', 'device'].includes(w.kind) && onCards.has(w.name))).map(idleWarning),
        ...(s.dispatch?.reroute || []).filter((line) => !moved.some((m) => line.endsWith(m)))
            .map((line) => el('li', { class: 'row' }, el('p', { class: 'row__note', text: line }))));
    const c = s.controls || { opt_in: [], paused: [] };
    const paused = new Map(c.paused.map((p) => [`${p.kind}:${p.name}`, p]));
    $('controls-meta').textContent = `${devices.length} device${devices.length === 1 ? '' : 's'} · ${c.paused.length} paused`;
    $('devices').replaceChildren(...(devices.length ? devices.map((d) => (d.checking ? el('li', { class: 'row', 'data-device': d.name, 'data-status': 'checking' },
        el('div', { class: 'row__name' }, d.name, el('span', { class: 'row__kind', text: `on ${d.machine}` })), chip('checking', 'idle')) : device(d, paused.get(`device:${d.name}`))))
        : [el('li', {}, empty('No Android devices', codeText('Press Pair a device, or run ', 'tutti devices pair', '.')))]));

    const discovered = s.discovered || [];
    $('discovered').replaceChildren(...(discovered.length ? discovered.map((d) => el('li', { class: 'row' },
        el('div', { class: 'row__name' }, d.name, el('span', { class: 'row__kind', text: `${d.type}${d.roles && d.roles.length ? ' · ' + d.roles.join(', ') : ''}` })),
        el('div', { class: 'row__stats' }, el('button', { class: 'btn btn--tonal', type: 'button', text: 'Set up', onclick: () => {
            // Need to implement setup action
            showBanner(el('strong', { text: 'Setup not implemented. ' }), 'Setup of discovered devices not implemented yet.');
        }}))))
        : [el('li', {}, empty('No new devices discovered', 'Everything on the network is set up.'))]));
    renderDeviceTools(s);   // devices.js: Pair a device, Pair a TV, Move devices
    const seen = new Set();
    const row = (kind, name, state) => {
        const k = `${kind}:${name}`;
        if (seen.has(k)) return null;
        seen.add(k);
        return pausable(kind, name, paused.get(k), state);
    };
    const workers = probedOr(s.workers.items, s.workers.configured, Boolean(s.workers.probed_at));
    const machines = probedOr(s.machines.items, s.machines.configured, probed);
    const ready = (w) => {
        if (w.checking) return 'checking';
        return w.ready ? 'ready' : 'down';
    };
    const local = (w) => w.kind !== 'cli';
    const leftover = (kinds) => c.paused.filter((p) => kinds.includes(p.kind) && !(p.kind === 'device' && onCards.has(p.name))).map((p) => row(p.kind, p.name));
    // a paused worker or account is out of the config's lists until resumed: listed from the switches, in its group
    const groups = [
        ['Work machines', 'No work machines in the config.', [
            ...machines.filter((m) => m.transport !== 'android').map((m) => row('machine', m.name, m.status))]],
        ['Termux machines', 'No Termux machines; each Android device pauses on its own card above.', [
            ...machines.filter((m) => m.transport === 'android').map((m) => row('machine', m.name, m.status)), ...leftover(['device'])]],
        ['Local agents', 'No local models (Ollama, llama.cpp) configured.', workers.filter(local).map((w) => row('worker', w.name, ready(w)))],
        ['Online agents', 'No online agents configured.', [
            ...workers.filter((w) => !local(w)).map((w) => row('worker', w.name, ready(w))),
            ...(s.usage?.accounts || []).map((a) => row('account', a.name, a.verdict)),
            ...(s.scouts?.items || []).map(scoutRow), ...leftover(['worker', 'account'])]],
    ];
    $('pausable').replaceChildren(...groups.flatMap(([title, none, items]) => {
        const list = items.filter(Boolean);
        return [el('h4', { class: 'card__sub', text: title }),
            el('ul', { class: 'list', 'aria-label': title }, ...(list.length ? list : [el('li', { class: 'row__note', text: none })]))];
    }));
    renderAutocompact(c.autocompact);
    $('optin').replaceChildren(...(c.opt_in.length ? c.opt_in.map(optIn)
        : [el('li', {}, empty('Nothing opt-in', 'Workers or accounts marked costs_money or opt_in, and Android lint runs, appear here.'))]));
}

// Autocompact: the % of a worker's context window at which it is compacted (POST /api/autocompact, kept in the state folder).
const COMPACT_HOW = { native: 'compacts by itself', tutti: 'compacted by Tutti', handover: 'restarted with a handoff' };

function renderAutocompact(a) {
    if (!a) return;
    const input = $('autocompact-pct');
    if (document.activeElement !== input) input.value = a.threshold_pct;
    $('autocompact-from').textContent = a.from === 'dashboard' ? `(set here; the config says ${tuttiPct(a.config_pct)})` : '(from the config)';
    const how = Object.entries(a.workers || {}).map(([n, m]) => `${n}: ${COMPACT_HOW[m] || m}`).join(' · ');
    $('autocompact-note').textContent = a.enabled ? (how || 'No workers configured.') : 'Off in the config (autocompact.enabled: false).';
}

async function saveAutocompact(pct) {
    try {
        const res = await fetch('/api/autocompact', {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN }, body: JSON.stringify({ threshold_pct: pct }),
        });
        const answer = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
        await refresh();
    } catch (err) {
        showBanner(el('strong', { text: 'The compaction threshold was not saved. ' }), err.message);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    $('autocompact-form').addEventListener('submit', (e) => {
        e.preventDefault();
        void saveAutocompact(Number($('autocompact-pct').value));
    });
    $('autocompact-reset').addEventListener('click', () => { void saveAutocompact(null); });
});

// ---- presence (tutti/presence.py): here, away or asleep since when, today's profile strip, and moving work off an absent target

function hereText(x, none) {
    return x === null ? none : `${tuttiShare(x)} here`;
}

function presenceBits(p) {
    if (!p) return null;
    const strip = el('span', { class: 'presence__strip', 'aria-label': 'today, hour by hour: ' + p.strip.map((x) => hereText(x, 'unknown')).join(', ') },
        ...p.strip.map((x, h) => {
            const cell = el('span', { class: 'presence__hour', title: String(h).padStart(2, '0') + ':00 ' + hereText(x, 'not seen yet') });
            cell.style.opacity = x === null ? '0.15' : String(0.25 + 0.75 * x);
            return cell;
        }));
    return el('div', { class: 'presence row__stats' }, el('span', { class: 'presence__label', 'data-state': p.state, text: p.label }), strip);
}

async function moveWork(body) {
    try {
        const res = await fetch('/api/work/move', {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN }, body: JSON.stringify(body),
        });
        const answer = await res.json().catch(() => ({}));
        if (res.status === 403) throw new Error(`only an admin can move work (${answer.error || 'your role is viewer'}): ask the owner, or sign in as an admin`);
        if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
        ui.notice = `Moved: ${(answer.done || []).join('; ')}`;
        await refresh();
    } catch (err) {
        showBanner(el('strong', { text: 'The work was not moved. ' }), err.message);
    }
}

function waitingRow(j, targets) {
    const pick = el('select', { 'aria-label': `Move ${j.title} to` },
        el('option', { value: 'auto', text: 'Best available' }), ...targets.map((t) => el('option', { value: t, text: t })));
    const bump = el('input', { type: 'checkbox', 'aria-label': 'First in its queue' });
    const go = el('button', { class: 'btn', type: 'button', text: 'Move now' });
    go.addEventListener('click', () => moveWork({ job: j.id, to: pick.value, bump: bump.checked }));
    const all = el('button', { class: 'btn btn--quiet', type: 'button', text: `Move all waiting on ${j.target.split(':')[1]}` });
    all.addEventListener('click', () => moveWork({ evacuate: j.target }));
    return el('li', { class: 'row' },
        el('div', { class: 'row__name' }, j.title, el('span', { class: 'row__kind', text: `${j.kind}${j.urgent ? ' · urgent' : ''}` })),
        el('p', { class: 'row__note', text: `Waits on ${j.target}: ${j.label}` }),
        el('div', { class: 'row__stats' }, pick, el('label', {}, bump, ' first in queue'), go, all));
}

function renderPresence(s) {
    const p = s.presence || { waiting: [], host: {} };
    const here = Object.entries(p.items || {}).filter(([id, it]) => it.state === 'present' && /^(device|machine):/.test(id)).map(([id]) => id);
    const waiting = p.waiting || [];
    $('waiting-h').hidden = !waiting.length;
    $('waiting').replaceChildren(...waiting.map((j) => waitingRow(j, here)));
    const warn = p.host?.warning || '';
    $('host-warning').hidden = !warn;   // in the Where Tutti runs card (where.js)
    $('host-warning').textContent = warn;
}
