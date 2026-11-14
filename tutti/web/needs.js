// Tutti dashboard: My questions (tutti/needs.py, docs/NEEDS.md). The questions only this user can answer, each with its
// options and default (a click answers it and the task resumes), and when they reach the user: in work hours only, or
// also outside them once N wait (urgent ones at once), never in their quiet hours. GET/POST /api/needs: their own only.
'use strict';

const NEEDS_TOKEN = document.querySelector('meta[name="tutti-token"]')?.content || '';

function needsEl(tag, props, ...kids) {
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

async function needsPost(body) {
    const res = await fetch('/api/needs', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': NEEDS_TOKEN }, body: JSON.stringify(body),
    });
    const answer = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
    return answer;
}

function needsQuestion(q, note) {
    return needsEl('li', { class: 'row' },
        needsEl('div', { class: 'row__name' }, `${q.id}${q.urgent ? ' (urgent)' : ''}: ${q.question}`,
            needsEl('span', { class: 'row__kind', text: `${q.project || ''} · ${q.why || ''}` })),
        needsEl('span', { class: 'row__stats' }, ...q.options.map((o) => needsEl('button', {
            class: o === q.default ? 'btn btn--tonal' : 'btn', type: 'button', 'data-control': 'needs-answer',
            title: o === q.default ? 'the default' : undefined,
            onclick: () => void needsPost({ answer: q.id, choice: o }).then((a) => { note.textContent = a.note; void loadNeeds(); })
                .catch((e) => { note.textContent = e.message; }),
        }, o === q.default ? `${o} (default)` : o))));
}

async function loadNeeds() {
    const list = document.getElementById('my-questions-list');
    if (!list) return;
    try {
        const res = await fetch('/api/needs', { headers: { Accept: 'application/json' } });
        if (!res.ok) return;
        const v = await res.json();
        const note = document.getElementById('my-notify-note');
        list.replaceChildren(...(v.open.length ? v.open.map((q) => needsQuestion(q, note))
            : [needsEl('li', { class: 'row__note', text: 'No questions for you: Tutti is working through the rest.' })]));
        if (document.activeElement?.form?.id !== 'my-notify-form') {
            document.getElementById('my-notify').value = v.settings.notify;
            document.getElementById('my-batch').value = v.settings.batch;
            document.getElementById('my-quiet').value = v.settings.quiet || '';
        }
    } catch {
        // the dashboard is restarting: the next poll tries again
    }
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('my-notify-form')?.addEventListener('submit', (e) => {
        e.preventDefault();
        const note = document.getElementById('my-notify-note');
        void needsPost({ notify: document.getElementById('my-notify').value, batch: Number(document.getElementById('my-batch').value || 3),
            quiet: document.getElementById('my-quiet').value })
            .then(() => { note.textContent = 'Saved.'; }).catch((err) => { note.textContent = err.message; });
    });
    void loadNeeds();
    setInterval(() => void loadNeeds(), 60000);
});
