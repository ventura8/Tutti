// Tutti dashboard: Welcome back (tutti/welcome.py). When this user returns after being away, their summary shows above
// the dashboard once: what shipped, what runs, what failed or was parked, usage, and a numbered list of what needs them
// (each with its exact step and a link). "What did I miss?" in the bar makes one at any time. Per user: GET /api/welcome
// answers this viewer's own summary only; "Turn off" keeps it off for them.
'use strict';

const WELCOME_TOKEN = document.querySelector('meta[name="tutti-token"]')?.content || '';
const welcomeState = { shownAt: null };

function welcomeEl(tag, props, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
        if (v === undefined || v === null || v === false) continue;
        if (k === 'text') node.textContent = v;
        else if (k === 'class') node.className = v;
        else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, String(v));
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) node.append(kid);
    return node;
}

async function welcomePost(action) {
    const res = await fetch('/api/welcome', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': WELCOME_TOKEN },
        body: JSON.stringify({ action }),
    });
    const answer = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
    return answer;
}

function welcomeGroup(title, items) {
    if (!items || !items.length) return null;
    return welcomeEl('details', { class: 'welcome__group', open: items.length <= 5 ? true : undefined },
        welcomeEl('summary', { text: `${title} (${items.length})` }),
        welcomeEl('ul', { class: 'list' }, ...items.slice(0, 15).map((i) => welcomeEl('li', { class: 'row__note', text: i.text }))));
}

function welcomeNeeds(needs) {
    if (!needs || !needs.length) return welcomeEl('p', { class: 'row__note', text: 'Nothing needs you.' });
    return welcomeEl('section', { class: 'welcome__needs' },
        welcomeEl('h3', { class: 'card__sub', text: `Needs you (${needs.length}): only you can do these` }),
        welcomeEl('ol', {}, ...needs.map((n) => welcomeEl('li', {},
            welcomeEl('strong', { text: n.title }), ` ${n.why ? '(' + n.why + ')' : ''} `,
            welcomeEl('code', { text: n.step }), ' ',
            n.link ? welcomeEl('a', { href: n.link, text: 'Go there' }) : null))));
}

function renderWelcome(summary, enabled) {
    const box = document.getElementById('welcome');
    if (!box || !summary) return;
    welcomeState.shownAt = summary.at;
    box.replaceChildren(...[   // replaceChildren writes a null as the text "null": the empty groups are dropped first
        welcomeEl('h2', { id: 'welcome-h', text: 'Welcome back' }),
        welcomeEl('p', { text: summary.intro }),
        welcomeNeeds(summary.needs),
        welcomeGroup('Shipped', summary.shipped), welcomeGroup('Running now', summary.running),
        welcomeGroup('Failed or parked', summary.failed), welcomeGroup('Usage', summary.usage),
        welcomeEl('p', { class: 'welcome__actions' },
            welcomeEl('button', { class: 'btn btn--tonal', type: 'button', id: 'welcome-read', onclick: () => {
                box.hidden = true;
                void welcomePost('read').catch(() => null);
            } }, 'Got it'),
            welcomeEl('button', { class: 'btn btn--text', type: 'button', id: 'welcome-toggle', onclick: () => {
                void welcomePost(enabled ? 'off' : 'on').then(() => { box.hidden = true; }).catch(() => null);
            } }, enabled ? 'Turn off welcome-back summaries' : 'Turn them on')),
    ].filter(Boolean));
    box.hidden = false;
}

async function loadWelcome() {
    try {
        const res = await fetch('/api/welcome', { headers: { Accept: 'application/json' } });
        if (!res.ok) return;
        const w = await res.json();
        const fresh = w.summary && w.summary.at !== welcomeState.shownAt;
        if (w.unread && fresh) renderWelcome(w.summary, w.enabled);
    } catch {
        // the dashboard is restarting: the next poll tries again
    }
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('welcome-miss')?.addEventListener('click', () => {
        void welcomePost('miss').then((a) => renderWelcome(a.summary, a.enabled)).catch(() => null);
    });
    void loadWelcome();
    setInterval(() => void loadWelcome(), 60000);
});
