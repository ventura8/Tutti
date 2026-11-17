// devices.js: the Pair a device and Move devices buttons of the Devices card. Pairing: POST /api/devices/pair starts it
// and the page asks its state every two seconds; the QR code is drawn here from the matrix the dashboard sends (no
// library). Moving: POST /api/devices/move with dry_run shows the plan (the move and the consolidation: other machines'
// stray connections) and what stops it; Move now asks to confirm, starts it in the background and shows its log. Uses app.js's helpers (el, $, refresh, showBanner, TOKEN).
'use strict';

async function devicesPost(path, body) {
    const res = await fetch(path, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN }, body: JSON.stringify(body),
    });
    const answer = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
    return answer;
}

function qrFromMatrix(m, label) {
    const ns = 'http://www.w3.org/2000/svg';
    const n = m.length + 8;
    const node = document.createElementNS(ns, 'svg');
    for (const [k, v] of Object.entries({ viewBox: `0 0 ${n} ${n}`, class: 'qr', role: 'img', 'aria-label': label })) node.setAttribute(k, v);
    const bg = document.createElementNS(ns, 'rect');
    for (const [k, v] of Object.entries({ width: n, height: n, fill: '#fff' })) bg.setAttribute(k, v);
    let d = '';
    m.forEach((row, y) => row.forEach((dark, x) => { if (dark) d += `M${x + 4} ${y + 4}h1v1h-1z`; }));
    const path = document.createElementNS(ns, 'path');
    path.setAttribute('d', d);
    path.setAttribute('fill', '#000');
    node.append(bg, path);
    return node;
}

const pairing = { timer: null, state: null };

function pairView(v) {
    const panel = $('device-tools');
    const parts = [el('ol', { class: 'row__note' }, ...v.steps.map((s) => el('li', { text: s })))];
    if (v.state === 'waiting' && v.matrix) parts.push(qrFromMatrix(v.matrix, 'Pairing QR code: scan it with Pair device with QR code'));
    if (v.state === 'waiting' && v.flow === 'code') {
        const input = el('input', { id: 'pair-code', inputmode: 'numeric', maxlength: '6', 'aria-label': 'The 6-digit code the device shows' });
        parts.push(el('form', { class: 'row', onsubmit: (e) => { e.preventDefault(); sendCode(input.value); } },
            input, el('button', { class: 'btn btn--small', type: 'submit', text: 'Pair' })));
    }
    parts.push(el('p', { class: 'row__note', role: 'status', text: v.message || 'Waiting for the device...' }));
    if (v.state === 'waiting') parts.push(el('button', { class: 'btn btn--small', type: 'button', text: 'Cancel', onclick: () => pairAction({ action: 'cancel' }) }));
    panel.replaceChildren(...parts);
    if (v.state !== 'waiting') {
        clearInterval(pairing.timer);
        pairing.timer = null;
        if (v.state === 'paired') refresh();
    }
}

async function pairAction(body) {
    try {
        const view = await devicesPost('/api/devices/pair', body);
        pairView(view);
        return view.state === 'waiting';
    } catch (err) {
        clearInterval(pairing.timer);
        pairing.timer = null;
        showBanner(el('strong', { text: 'Pairing stopped. ' }), err.message);
        return false;
    }
}

function sendCode(code) {
    pairAction({ action: 'code', code: code.trim() });
}

async function startPair(kind) {
    const waiting = await pairAction(kind ? { action: 'start', kind } : { action: 'start' });
    if (waiting && !pairing.timer) pairing.timer = setInterval(() => pairAction({ action: 'status' }), 2000);
}

const moving = { timer: null };

function moveProgress(out) {
    // the move runs in the background: its log's last lines every two seconds until it ends, then the result
    const log = el('pre', { id: 'move-log', class: 'row__note', tabindex: '0', 'aria-label': 'The move\'s log' });
    const said = el('p', { id: 'move-state', class: 'row__note', role: 'status', text: 'Moving…' });
    out.replaceChildren(said, log);
    const tick = async () => {
        try {
            const r = await devicesPost('/api/devices/move', { action: 'status' });
            log.textContent = r.lines.join('\n');
            if (!r.running) {
                clearInterval(moving.timer);
                moving.timer = null;
                said.textContent = r.exit === 0 ? 'Done: the move finished.' : `The move ended with exit ${r.exit}: the log says why.`;
                refresh();
            }
        } catch (err) {
            clearInterval(moving.timer);
            moving.timer = null;
            said.textContent = `The progress could not be read: ${err.message}`;
        }
    };
    moving.timer = setInterval(tick, 2000);
    tick();
}

function moveConfirm(out, ask, r) {
    // a confirm step before anything changes: what moves, what is disconnected, which runners switch
    const strays = Object.values(r.strays || {}).reduce((n, l) => n + l.length, 0);
    const what = [r.moves.length ? `move ${r.moves.join(', ')} to ${ask.to}` : '',
        strays ? `disconnect ${strays} stray connection(s) on other machines` : '',
        (r.runners || []).length ? `switch ${r.runners.join(', ')} to ${ask.to}'s adb` : ''].filter(Boolean).join('; ');
    const cancel = el('button', { id: 'move-cancel', class: 'btn btn--small', type: 'button', text: 'Cancel', onclick: () => {
        out.replaceChildren(el('p', { class: 'row__note', role: 'status', text: 'Cancelled: nothing changed.' }));
    } });
    const yes = el('button', { id: 'move-confirm', class: 'btn btn--small', type: 'button', text: 'Yes, do it', onclick: () => {
        devicesPost('/api/devices/move', { ...ask, dry_run: false }).then(() => moveProgress(out),
            (err) => out.replaceChildren(el('p', { class: 'row__note', role: 'alert', text: `The move did not start: ${err.message}` })));
    } });
    const box = el('div', { id: 'move-ask', role: 'group', 'aria-labelledby': 'move-ask-text' },
        el('p', { id: 'move-ask-text', class: 'row__note', text: `Confirm: ${what}?` }), yes, cancel);
    out.append(box);
    yes.focus();
}

function moveForm(s) {
    const machines = (s.machines?.items || []).map((m) => m.name);
    const devs = s.devices?.items || [];
    const to = el('select', { id: 'move-to' }, ...machines.map((m) => el('option', { value: m, text: m })));
    const boxes = devs.map((d) => el('label', { class: 'row__note' },
        el('input', { type: 'checkbox', value: d.name, checked: true }), ` ${d.name} (on ${d.machine})`));
    const out = el('div', { id: 'move-out', 'aria-live': 'polite' });
    const chosen = () => boxes.map((b) => b.firstChild).filter((c) => c.checked).map((c) => c.value);
    const plan = async () => {
        const ask = { to: to.value, devices: chosen() };
        out.replaceChildren(el('p', { class: 'row__note', role: 'status', text: 'Checking every machine…' }));
        try {
            const r = await devicesPost('/api/devices/move', { ...ask, dry_run: true });
            const go = el('button', { id: 'move-go', class: 'btn btn--small', type: 'button', text: 'Move now',
                disabled: r.problems.length > 0 || !r.todo, onclick: () => { go.disabled = true; moveConfirm(out, ask, r); } });
            out.replaceChildren(el('h3', { class: 'row__name', text: 'The plan (dry run: nothing changed yet)' }),
                el('ol', { id: 'move-plan', class: 'row__note' }, ...r.plan.map((l) => el('li', { text: l }))),
                ...r.problems.map((p) => el('p', { class: 'row__note', role: 'alert', text: `Cannot move yet: ${p}` })), go);
        } catch (err) {
            out.replaceChildren(el('p', { class: 'row__note', role: 'alert', text: err.message }));
        }
    };
    $('device-tools').replaceChildren(
        el('div', { class: 'row' }, el('label', { class: 'row__name', for: 'move-to', text: 'Move to' }), to),
        el('fieldset', { class: 'row__note' }, el('legend', { text: 'Devices' }), ...boxes),
        el('button', { id: 'move-plan-btn', class: 'btn btn--small', type: 'button', text: 'Show the plan', onclick: () => plan() }), out);
    to.focus();
}

function renderDeviceTools(s) {
    pairing.state = s;
    const bar = $('device-actions');
    if (bar.childElementCount) return;
    bar.append(
        el('button', { class: 'btn btn--small', type: 'button', text: 'Pair a device', title: 'A phone or tablet: scan a QR code', onclick: () => startPair() }),
        el('button', { class: 'btn btn--small', type: 'button', text: 'Pair a TV', title: 'A TV has no camera: type the code it shows', onclick: () => startPair('tv') }),
        el('button', { class: 'btn btn--small', type: 'button', text: 'Move devices', title: 'Move devices to another machine', onclick: () => moveForm(pairing.state) }));
}

if (typeof module !== 'undefined') module.exports = { qrFromMatrix };
