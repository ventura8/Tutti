// Tutti dashboard: the Queued list beside Running jobs (tutti/waitlist.py). Everything waiting, grouped by project in
// priority order, each with its size, where it goes, its estimated start and why it waits; an admin's actions (Run now,
// Move to, Cancel, Priority up or down) post to /api/queue. Uses app.js's helpers (el, $, chip, refresh, showBanner, TOKEN).

const QUEUE_ACTIONS = { run: 'Run now', up: 'Priority up', down: 'Priority down', cancel: 'Cancel' };

async function queueAction(body, failText) {
    try {
        const res = await fetch('/api/queue', {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN }, body: JSON.stringify(body),
        });
        const answer = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
        await refresh();
    } catch (err) {
        showBanner(el('strong', { text: `${failText} ` }), err.message);
    }
}

function queueMove(it, ladder) {
    const pick = el('select', { class: 'pick', 'aria-label': `Move ${it.id} to` },
        ...ladder.map((n) => el('option', { value: n, text: n, selected: n === it.to })));
    return [pick, el('button', { class: 'btn btn--text', type: 'button', text: 'Move to…',
        onclick: () => queueAction({ kind: it.kind, id: it.id, action: 'move', to: pick.value }, `${it.id} was not moved.`) })];
}

let queueBoard = false;

function graphEdges(id, node, graph) {
    const target = (tid) => `${tid} (${graph[tid]?.status || 'missing'})`;
    const edges = [
        [(node.depends_on || []), 'depends on', '→'],
        [(node.together_with || []), 'grouped with', '↔'],
        [node.merged_into ? [node.merged_into] : [], 'merged into', '→'],
    ];
    return edges.flatMap(([ids, label, arrow]) => ids.map((tid) =>
        el('li', { text: `${id} ${arrow} ${target(tid)} · ${label}` })));
}

function dependencyGraph(id, graph, section = 'queue') {
    if (!graph || !Object.hasOwn(graph, id)) return null;
    const pending = [id];
    const seen = new Set();
    const edges = [];
    for (const tid of pending) {
        if (seen.has(tid) || !Object.hasOwn(graph, tid)) continue;
        seen.add(tid);
        const node = graph[tid];
        edges.push(...graphEdges(tid, node, graph));
        pending.push(...node.depends_on, ...node.together_with);
        if (node.merged_into) pending.push(node.merged_into);
    }
    return el('details', { keep: `deps:${section}:${id}` },
        el('summary', { text: 'Dependency graph' }),
        edges.length ? el('ul', { class: 'list' }, ...edges) : el('p', { class: 'row__note', text: 'No task links.' }));
}

function queueItem(it, ladder, graph) {
    const acts = (it.actions || []).filter((a) => a !== 'move').map((a) => el('button', { class: 'btn btn--text', type: 'button',
        'data-queue-action': a, text: QUEUE_ACTIONS[a],
        onclick: () => queueAction({ kind: it.kind, id: it.id, action: a }, `${QUEUE_ACTIONS[a]} did not work for ${it.id}.`) }));
    if ((it.actions || []).includes('move') && ladder.length) acts.push(...queueMove(it, ladder));
    return el('div', { class: 'row', 'data-queued': it.id, 'data-kind': it.kind },
        el('div', { class: 'row__name', text: it.title }),
        el('p', { class: 'row__note', text: `${it.kind} ${it.id} · size ${it.size} · goes to ${it.to} · starts ${it.eta}` }),
        el('p', { class: 'row__note', 'data-why': '', text: `Waiting: ${it.why}` }),
        it.group?.length ? el('p', { class: 'row__note', text: `Group: ${it.group.join(', ')}` }) : null,
        it.kind === 'task' ? dependencyGraph(it.id, graph) : null,
        acts.length ? el('div', { class: 'row__stats' }, ...acts) : '');
}

function queueLanes(q, row) {
    return el('div', { class: 'queue-board' }, ...(q.board || []).map((g) =>
        el('section', {},
            el('h3', { class: 'card__sub queue__group', text: g.lane }),
            ...(g.items.length ? g.items.map(row) : [el('p', { class: 'row__note', text: 'No tasks.' })]))));
}

function queueProjects(q, row) {
    const rows = q.groups.flatMap((g) => [
        el('h3', { class: 'card__sub queue__group', 'aria-label': `${g.project} (priority ${g.priority})`,
            text: `${g.project} · priority ${g.priority} · ${g.items.length} queued` }),
        ...g.items.map(row)]);
    if (!rows.length) return [el('p', { class: 'row__note', text: 'Nothing is waiting.' })];
    const limit = (typeof ui === 'object' && ui.fit?.queued) || 8;
    return typeof more === 'function' ? more('queued', rows, limit, 'div') : rows;
}

function renderQueue(s) {
    const q = s.queue;
    if (!q || !$('queued')) return;
    $('queued-meta').textContent = q.header;
    const ladder = q.can_act ? (s.autopilot?.ladder || []) : [];
    const row = (it) => queueItem(it, ladder, q.graph);
    const toggle = el('button', { class: 'btn btn--text', type: 'button', text: 'Board view',
        'aria-pressed': String(queueBoard), onclick: () => {
            queueBoard = !queueBoard;
            renderQueue(s);
            $('queued').querySelector('button')?.focus();
        } });
    $('queued').replaceChildren(toggle, ...(queueBoard ? [queueLanes(q, row)] : queueProjects(q, row)));
}
