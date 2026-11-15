// Tutti dashboard: My GitHub (docs/GITHUB.md). Connect GitHub runs GitHub's device flow: a code to enter at
// github.com/login/device (and its QR), then the page waits for GitHub. The token stays on the dashboard's computer.
'use strict';

const GITHUB_TOKEN = document.querySelector('meta[name="tutti-token"]')?.content || '';
let githubTimer = null;

async function githubPost(action) {
    const res = await fetch('/api/github', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': GITHUB_TOKEN },
        body: JSON.stringify({ action }),
    });
    const answer = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}: reload the page and try again`);
    return answer;
}

function githubNote(text) {
    $('my-github-note').textContent = text;
}

function githubShow(st) {
    const how = st.connected ? `Connected as ${st.login || 'your account'}.`
        : st.source === 'household' ? 'Not connected: Tutti uses the household\'s connection for you.'
        : st.source === 'gh' ? 'Not connected: Tutti uses the GitHub CLI\'s sign-in on the dashboard\'s computer.'
        : 'Not connected: Tutti cannot reach GitHub for you yet.';
    const parts = [el('p', { class: 'row__note', text: how })];
    if (st.connected) {
        parts.push(el('a', { href: st.install_url, target: '_blank', rel: 'noopener noreferrer', text: 'Choose the repositories Tutti may use' }));
    }
    $('my-github-body').replaceChildren(...parts);
    $('my-github-connect').hidden = st.connected || !st.app;
    $('my-github-disconnect').hidden = !st.connected;
    if (!st.app && !st.connected) githubNote('Connect GitHub arrives with an update of Tutti; until then, sign in the GitHub CLI (gh auth login).');
}

async function githubRefresh() {
    try {
        githubShow(await githubPost('status'));
    } catch (err) {
        githubNote(err.message);
    }
}

async function githubWait(interval) {
    try {
        const done = await githubPost('poll');
        if (done.pending) {
            githubTimer = setTimeout(() => githubWait(interval), interval * 1000);
            return;
        }
        githubNote(`Connected as ${done.login}.`);
        githubRefresh();
    } catch (err) {
        githubNote(err.message);
    }
}

async function githubConnect() {
    clearTimeout(githubTimer);
    try {
        const flow = await githubPost('start');
        const link = el('a', { href: flow.verification_url, target: '_blank', rel: 'noopener noreferrer', text: flow.verification_url });
        const steps = el('p', { class: 'row__note' }, `Open `, link, ` on any device and enter `, el('strong', { text: flow.user_code }), '.');
        const qr = typeof qrSvg === 'function' ? qrSvg(flow.qr_url, 'QR code of GitHub\'s device page') : null;
        $('my-github-body').replaceChildren(steps, ...(qr ? [qr] : []));
        githubNote('Waiting for GitHub…');
        githubTimer = setTimeout(() => githubWait(flow.interval), flow.interval * 1000);
    } catch (err) {
        githubNote(err.message);
    }
}

async function githubDisconnect() {
    try {
        await githubPost('disconnect');
        githubNote('Disconnected here. To revoke it on GitHub too, open github.com/settings/apps/authorizations.');
        githubRefresh();
    } catch (err) {
        githubNote(err.message);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    if (!$('my-github')) return;
    $('my-github-connect').addEventListener('click', githubConnect);
    $('my-github-disconnect').addEventListener('click', githubDisconnect);
    githubRefresh();
});
