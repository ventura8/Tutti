// Tutti dashboard: the Tasks tab (#tasks, t307, docs/DASHBOARD.md *Tasks*). Subtabs, one per status bucket from
// GET /api/tasks, P0 first; a task opens to everything it has (queue.js loadTaskDetail). Uses app.js's el().
'use strict';

const TASKS = { data: null, tab: 'active', loading: false };

function taskRow(t) {
    const key = `tasks:${t.id}`;
    const open = typeof ui === 'object' && ui.open?.has(key);
    const body = el('div', { class: 'qdetail' });
    if (open && typeof loadTaskDetail === 'function') void loadTaskDetail(t.id, body, false);
    const facts = [t.id, t.repo, t.priority, t.size && `size ${t.size}`, t.who].filter(Boolean).join(' · ');
    return el('li', { class: 'row', 'data-task': t.id },
        el('details', { class: 'qitem', open, ontoggle: (e) => {
            const shown = e.currentTarget.open;
            if (typeof ui === 'object' && ui.open) {
                if (shown === ui.open.has(key)) return;   // a redraw restoring the open state: nothing to fetch
                if (shown) ui.open.add(key); else ui.open.delete(key);
            }
            if (shown && typeof loadTaskDetail === 'function') void loadTaskDetail(t.id, body);
        } }, el('summary', { class: 'row__name', text: t.title, 'aria-label': `${t.id}: ${t.title}. Show everything it has` }), body),
        el('p', { class: 'row__note', text: facts }),
        t.why ? el('p', { class: 'row__note', text: `Waiting: ${t.why}` }) : null);
}

function tasksTabs(buckets) {
    return buckets.map((b) => el('button', { class: 'btn btn--text tasks__tab', type: 'button', role: 'tab', id: `tasks-tab-${b.key}`,
        'aria-selected': String(b.key === TASKS.tab), 'aria-controls': 'tasks-panel', tabindex: b.key === TASKS.tab ? '0' : '-1',
        text: `${b.label} (${b.count})`, onclick: () => { TASKS.tab = b.key; drawTasks(true); } }));
}

// Arrow keys move between subtabs (the tablist pattern), Home and End to the ends
function tasksKey(e) {
    const keys = TASKS.data.buckets.map((b) => b.key);
    const i = keys.indexOf(TASKS.tab);
    const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: keys.length - 1 }[e.key];
    if (next === undefined) return;
    e.preventDefault();
    TASKS.tab = keys[(next + keys.length) % keys.length];
    drawTasks(true);
}

function drawTasks(focus = false) {
    const tabs = document.getElementById('tasks-tabs');
    const panel = document.getElementById('tasks-panel');
    if (!tabs || !panel || !TASKS.data) return;
    const buckets = TASKS.data.buckets;
    tabs.replaceChildren(...tasksTabs(buckets));
    tabs.onkeydown = tasksKey;
    const b = buckets.find((x) => x.key === TASKS.tab) || buckets[0];
    panel.setAttribute('aria-labelledby', `tasks-tab-${b.key}`);
    panel.replaceChildren(b.items.length ? el('ul', { class: 'list' }, ...b.items.map(taskRow))
        : el('p', { class: 'row__note', text: `No task is ${b.label.toLowerCase()}.` }));
    if (focus) document.getElementById(`tasks-tab-${b.key}`)?.focus();
}

async function loadTasks() {
    if (TASKS.loading) return;
    TASKS.loading = true;
    try {
        const res = await fetch('/api/tasks', { cache: 'no-store' });
        const d = await res.json();
        if (!res.ok) throw new Error(d.error || `the dashboard answered ${res.status}`);
        TASKS.data = d;
        drawTasks();
    } catch (err) {
        document.getElementById('tasks-panel')?.replaceChildren(el('p', { class: 'row__note', role: 'alert', text: `Cannot show the tasks: ${err.message}` }));
    } finally {
        TASKS.loading = false;
    }
}

if (typeof document === 'object' && document.addEventListener) {
    const maybe = () => { if (typeof tuttiView === 'function' && tuttiView(location.hash) === 'tasks') void loadTasks(); };
    document.addEventListener('DOMContentLoaded', maybe);
    window.addEventListener('hashchange', maybe);
}

if (typeof module !== 'undefined') module.exports = { taskRow, tasksTabs, TASKS };
