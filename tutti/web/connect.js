// Tutti dashboard: the "Connect your agent" card (docs/CONNECT.md). Uses app.js's helpers (el, $, chip, showBanner, TOKEN).
// Every button is a POST to /api/connect; Test starts the agent's own configured MCP server and lists Tutti's tools.
'use strict';

const CONNECT_TONE = { connected: 'ok', 'needs-restart': 'warn', 'not-connected': 'idle', 'not-installed': 'idle', cannot: 'bad', error: 'bad' };
const CONNECT_TEXT = {
    connected: 'connected', 'needs-restart': 'needs restart', 'not-connected': 'not connected',
    'not-installed': 'not installed', cannot: "can't connect locally", error: 'error',
};
const connectUi = { tests: {} };

async function connectPost(body) {
    const res = await fetch('/api/connect', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN }, body: JSON.stringify(body),
    });
    const answer = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
    return answer;
}

function connectButton(text, action, agent, tone) {
    const b = el('button', { class: `btn btn--${tone || 'tonal'}`, type: 'button', text });
    b.addEventListener('click', () => { void connectAct(action, agent, b); });
    return b;
}

function connectRow(r) {
    const t = connectUi.tests[r.agent];
    const buttons = [];
    if (r.status === 'not-connected' || r.status === 'error') buttons.push(connectButton('Connect', 'connect', r.agent, 'filled'));
    if (r.status === 'connected' || r.status === 'needs-restart') {
        buttons.push(connectButton('Test', 'test', r.agent));
        buttons.push(connectButton('Disconnect', 'undo', r.agent));
    }
    return el('li', { class: 'row' },
        el('div', { class: 'row__name' }, r.label, el('span', { class: 'row__kind', text: r.agent })),
        chip(CONNECT_TEXT[r.status] || r.status, CONNECT_TONE[r.status]),
        r.note ? el('p', { class: 'row__note', text: r.note }) : '',
        t ? el('p', { class: 'row__note', text: `Test: ${t.ok ? 'ok' : 'failed'}, ${t.text}` }) : '',
        buttons.length ? el('div', { class: 'row__stats' }, ...buttons) : '');
}

async function renderConnect() {
    try {
        const answer = await connectPost({ action: 'status' });
        $('connect').replaceChildren(...answer.agents.map(connectRow));
    } catch (err) {
        $('connect-meta').textContent = `The agents' state could not be read: ${err.message}`;
    }
}

async function connectAct(action, agent, button) {
    if (button) button.disabled = true;
    try {
        const answer = await connectPost({ action, agent });
        if (action === 'test') connectUi.tests[agent] = answer;
        else delete connectUi.tests[agent];
    } catch (err) {
        showBanner(el('strong', { text: `${agent}: ${action} did not work. ` }), err.message);
    }
    await renderConnect();
}

async function connectAll() {
    const answer = await connectPost({ action: 'status' }).catch(() => ({ agents: [] }));
    for (const r of answer.agents.filter((x) => x.status === 'not-connected')) await connectAct('connect', r.agent);
    await renderConnect();
}

document.addEventListener('DOMContentLoaded', () => {
    $('connect-all').addEventListener('click', () => { void connectAll(); });
    void renderConnect();
});
