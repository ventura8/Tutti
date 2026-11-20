// Tutti dashboard: the Needs you tab (#needs; tutti/needs.py tab_view, docs/NEEDS.md). Every open question only this user can
// answer, with its why, exact next action, its options (the default marked) or their own words, and a jump to the parked
// task; then the rest of their needs-you list (set-up steps, readings, proposals) with each step. Tutti-resolvable questions
// stay off it until Tutti has tried them (t244). GET /api/needs/tab, answers through POST /api/needs: their own only.
'use strict';

// ---- pure (a node test shares them)

function needsTabMeta(v) {
    const n = v.questions.length + v.items.length;
    const busy = v.resolving ? ` Tutti is still trying ${v.resolving} more itself.` : '';
    if (!n) return `Nothing needs you: Tutti is working through the rest.${busy}`;
    return `${n} thing${n === 1 ? '' : 's'} only you can do; the rest of the work goes on meanwhile.${busy}`;
}

function needsTabCount(v) {
    const n = v.questions.length + v.items.length;
    return n ? ` (${n})` : '';
}

// Where an item's "Go there" leads: a Settings anchor as it is, anything else the dashboard (never a link from elsewhere).
function needsTabHref(link) {
    return /^\/?#settings(\/|$)/.test(link || '') ? link.replace(/^\//, '') : '#dashboard';
}

// ---- the page

function ntNote(text) {
    document.getElementById('needs-note').textContent = text;
}

async function ntSend(id, choice) {
    try {
        ntNote((await needsPost({ answer: id, choice })).note);
    } catch (err) {
        ntNote(err.message);
    }
    void loadNeedsTab();
    if (typeof loadNeeds === 'function') void loadNeeds();
}

function ntGoTask(tid) {
    const go = () => requestAnimationFrame(() => {
        if (typeof showTask === 'function' && showTask(tid)) return;
        if (typeof showBanner !== 'function') return;
        showBanner(needsEl('strong', { text: `Task ${tid} is not listed. ` }), 'It was answered, retried or dropped meanwhile.');
    });
    if ((location.hash || '').startsWith('#dashboard')) go();
    else {
        window.addEventListener('hashchange', go, { once: true });
        location.hash = '#dashboard';
    }
}

function ntJump(tid) {
    return tid ? needsEl('button', { class: 'btn btn--text', type: 'button', onclick: () => ntGoTask(tid) }, `Open task ${tid}`) : null;
}

function ntAnswer(q) {
    const own = needsEl('input', { type: 'text', maxlength: '300', size: '24' });
    const form = needsEl('form', { class: 'needs-answer' },
        ...q.options.map((o) => needsEl('button', { class: o === q.default ? 'btn btn--tonal' : 'btn', type: 'button',
            onclick: () => void ntSend(q.id, o) }, o === q.default ? `${o} (default)` : o)),
        needsEl('label', {}, 'Or in your own words (kept as a note; the task retries) ', own),
        needsEl('button', { class: 'btn btn--text', type: 'submit' }, 'Answer'));
    form.addEventListener('submit', (e) => {
        e.preventDefault();
        if (own.value.trim()) void ntSend(q.id, own.value.trim());
    });
    return form;
}

function ntQuestion(q) {
    return needsEl('li', { class: 'row row--attention', 'data-tone': q.urgent ? 'bad' : 'warn' },
        needsEl('div', { class: 'row__name' }, `${q.id}${q.urgent ? ' (urgent)' : ''}: ${q.question}`,
            needsEl('span', { class: 'row__kind', text: [q.project, q.title].filter(Boolean).join(' · ') })),
        needsEl('p', { class: 'row__note', text: `Why: ${q.why || 'not given'}` }),
        needsEl('p', { class: 'row__note', text: `Next: ${q.next_action}` }),
        ntAnswer(q), ntJump(q.task));
}

function ntItem(i) {
    return needsEl('li', { class: 'row row--attention', 'data-tone': 'warn' },
        needsEl('div', { class: 'row__name', text: i.title }),
        needsEl('p', { class: 'row__note', text: `Why: ${i.why || 'only you can do it'}` }),
        needsEl('p', { class: 'row__note', text: `Next: ${i.step}` }),
        i.task ? ntJump(i.task) : needsEl('a', { class: 'link', href: needsTabHref(i.link) }, 'Go there'));
}

function ntRender(v) {
    document.getElementById('needs-count').textContent = needsTabCount(v);
    document.getElementById('needs-meta').textContent = needsTabMeta(v);
    document.getElementById('needs-questions').replaceChildren(...(v.questions.length ? v.questions.map(ntQuestion)
        : [needsEl('li', { class: 'row__note', text: 'No questions wait for you.' })]));
    document.getElementById('needs-other').replaceChildren(...(v.items.length ? v.items.map(ntItem)
        : [needsEl('li', { class: 'row__note', text: 'Nothing else waits for you.' })]));
}

async function loadNeedsTab() {
    if (document.activeElement?.closest?.('#needs-questions input')) return;   // never wipe words being typed
    try {
        const res = await fetch('/api/needs/tab', { headers: { Accept: 'application/json' } });
        if (res.ok) ntRender(await res.json());
    } catch {
        // the dashboard is restarting: the next poll tries again
    }
}

if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('DOMContentLoaded', () => {
        document.getElementById('needs-refresh')?.addEventListener('click', () => void loadNeedsTab());
        window.addEventListener('hashchange', () => { if ((location.hash || '').startsWith('#needs')) void loadNeedsTab(); });
        void loadNeedsTab();
        setInterval(() => void loadNeedsTab(), 60000);
    });
}

if (typeof module !== 'undefined') module.exports = { needsTabMeta, needsTabCount, needsTabHref };
