// devices.js: the Pair a device and Move devices buttons of the Devices card. Pairing: POST /api/devices/pair starts it
// and the page asks its state every two seconds; the QR code is drawn here from the matrix the dashboard sends (no
// library). Moving: POST /api/devices/move with dry_run shows the plan and what stops it; Move now starts it in the
// background. Uses app.js's helpers (el, $, refresh, showBanner, TOKEN).
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

function moveForm(s) {
    const machines = (s.machines?.items || []).map((m) => m.name);
    const devs = s.devices?.items || [];
    const to = el('select', { id: 'move-to', 'aria-label': 'Move to the machine' }, ...machines.map((m) => el('option', { value: m, text: m })));
    const boxes = devs.map((d) => el('label', { class: 'row__note' },
        el('input', { type: 'checkbox', value: d.name, checked: true }), ` ${d.name} (on ${d.machine})`));
    const out = el('div', { role: 'status' });
    const chosen = () => boxes.map((b) => b.firstChild).filter((c) => c.checked).map((c) => c.value);
    const plan = async (key) => {
        try {
            const r = await devicesPost('/api/devices/move', { to: to.value, devices: chosen(), dry_run: true, key });
            const move = el('button', { class: 'btn btn--small', type: 'button', text: 'Move now', disabled: r.problems.length > 0,
                onclick: () => devicesPost('/api/devices/move', { to: to.value, devices: chosen(), dry_run: false, key })
                    .then((a) => showBanner(el('strong', { text: 'Moving. ' }), `The log is ${a.log}; the card updates when it is done.`),
                        (err) => showBanner(el('strong', { text: 'The move did not start. ' }), err.message)) });
            out.replaceChildren(el('ol', { class: 'row__note' }, ...r.plan.map((l) => el('li', { text: l }))),
                ...r.problems.map((p) => el('p', { class: 'row__note', text: `Cannot move yet: ${p}` })), move);
        } catch (err) {
            out.replaceChildren(el('p', { class: 'row__note', text: err.message }));
        }
    };
    $('device-tools').replaceChildren(el('div', { class: 'row' }, el('span', { class: 'row__name', text: 'Move to' }), to), ...boxes,
        el('button', { class: 'btn btn--small', type: 'button', text: 'Show the plan', onclick: () => plan() }), out);
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
