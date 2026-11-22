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

// t237: a queued task opens (click, Enter or Space on its title) to everything it has, read from /api/task/<id> when opened
const TASK_DETAIL = new Map();   // id -> the last answer, so a redraw every few seconds does not fetch again

function detailList(rows) {
    return el('dl', { class: 'qdetail__facts' }, ...rows.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]));
}

// The expanded body from one /api/task answer (pure but for el(): node tests build it with a fake el)
function taskDetailNodes(d) {
    const out = [detailList(d.facts || [])];
    if (d.pr) out.push(el('p', {}, el('a', { class: 'link', href: d.pr, target: '_blank', rel: 'noopener noreferrer', text: 'Pull request' })));
    if ((d.links || []).length) out.push(el('h4', { class: 'qdetail__head', text: 'Dependencies' }), el('ul', {}, ...d.links.map((l) => el('li', { text: l }))));
    if ((d.criteria || []).length) out.push(el('h4', { class: 'qdetail__head', text: 'Done when' }), el('ul', {}, ...d.criteria.map((c) => el('li', { text: c }))));
    if ((d.attempts || []).length) {
        out.push(el('h4', { class: 'qdetail__head', text: `Attempts (${d.attempts_total}, newest first)` }),
            el('ol', { class: 'qdetail__attempts' }, ...d.attempts.map((a) => el('li', { 'data-ok': String(a.ok),
                text: `${a.at} · ${a.step || 'unknown step'} · ${a.ok ? 'ok' : 'failed'}${a.why ? ': ' + a.why : ''}` }))));
    }
    if ((d.notes || []).length) {
        out.push(el('h4', { class: 'qdetail__head', text: `Notes (${d.notes.length} of ${d.notes_total}, newest last)` }),
            el('ul', { class: 'qdetail__notes' }, ...d.notes.map((n) => el('li', { text: n }))));
    }
    return out;
}

async function loadTaskDetail(id, box, fresh = true) {
    if (TASK_DETAIL.has(id)) {
        box.replaceChildren(...taskDetailNodes(TASK_DETAIL.get(id)));   // drawn at once; a reopen still asks for the latest
        if (!fresh) return;
    } else box.replaceChildren(el('p', { class: 'row__note', text: 'Loading…' }));
    try {
        const res = await fetch(`/api/task/${encodeURIComponent(id)}`, { cache: 'no-store' });
        const d = await res.json();
        if (!res.ok) throw new Error(d.error || `the dashboard answered ${res.status}`);
        TASK_DETAIL.set(id, d);
        box.replaceChildren(...taskDetailNodes(d));
    } catch (err) {
        box.replaceChildren(el('p', { class: 'row__note', role: 'alert', text: `Cannot show ${id}: ${err.message}` }));
    }
}

function queueItem(it, ladder) {
    const acts = (it.actions || []).filter((a) => a !== 'move').map((a) => el('button', { class: 'btn btn--text', type: 'button',
        'data-queue-action': a, text: QUEUE_ACTIONS[a],
        onclick: () => queueAction({ kind: it.kind, id: it.id, action: a }, `${QUEUE_ACTIONS[a]} did not work for ${it.id}.`) }));
    if ((it.actions || []).includes('move') && ladder.length) acts.push(...queueMove(it, ladder));
    const note = el('p', { class: 'row__note', text: `${it.kind} ${it.id} · size ${it.size} · goes to ${it.to} · starts ${it.eta}` });
    const why = el('p', { class: 'row__note', 'data-why': '', text: `Waiting: ${it.why}` });
    const tail = acts.length ? el('div', { class: 'row__stats' }, ...acts) : '';
    if (it.kind !== 'task') return el('div', { class: 'row', 'data-queued': it.id, 'data-kind': it.kind }, el('div', { class: 'row__name', text: `${it.title}` }), note, why, tail);
    const key = `task:${it.id}`;
    const open = typeof ui === 'object' && ui.open?.has(key);
    const body = el('div', { class: 'qdetail' });
    const box = el('details', { class: 'qitem', open, ontoggle: (e) => {
        const shown = e.currentTarget.open;
        if (typeof ui === 'object' && ui.open) {
            if (shown) ui.open.add(key); else ui.open.delete(key);
            if (typeof saveUi === 'function') saveUi();
        }
        if (shown) void loadTaskDetail(it.id, body);
    } }, el('summary', { class: 'row__name', text: it.title, 'aria-label': `${it.id}: ${it.title}. Show everything it has` }), body);
    if (open) void loadTaskDetail(it.id, body, false);   // a redraw reuses what it has; opening it again fetches
    return el('div', { class: 'row', 'data-queued': it.id, 'data-kind': it.kind }, box, note, why, tail);
}

// t254: room somewhere while queued work waits, with the reasons the queue gives (pure but for el(): node tests)
function idleNote(w) {
    if (!w) return null;
    return el('div', { class: 'idlework', role: 'note', 'data-idle-with-work': '' },
        el('p', { class: 'idlework__head', text: w.headline }),
        el('ul', { class: 'idlework__list' },
            ...w.idle.map((r) => el('li', { text: `${r.name} (${r.kind}): ${r.room}` })),
            ...w.reasons.map(([why, n]) => el('li', { class: 'idlework__why', text: `${n} waiting: ${why}` }))));
}

function renderQueue(s) {
    const q = s.queue;
    if (!q || !$('queued')) return;
    $('queued-meta').textContent = q.header;
    const idle = $('idle-with-work');
    if (idle) idle.replaceChildren(...[idleNote(s.idle_with_work)].filter(Boolean));
    const ladder = s.autopilot?.ladder || [];
    // as many rows as fit beside the main-agent and workers column (app.js measureFit), then "Show more" for the rest
    const rows = q.groups.flatMap((g) => [
        el('h3', { class: 'card__sub queue__group', 'aria-label': `${g.project} (priority ${g.priority})`,
            text: `${g.project} · priority ${g.priority} · ${g.items.length} queued` }),
        ...g.items.map((it) => queueItem(it, q.can_act ? ladder : []))]);
    const limit = (typeof ui === 'object' && ui.fit?.queued) || 8;
    const shown = typeof more === 'function' ? more('queued', rows, limit, 'div') : rows;
    const box = $('queued');
    const active = box.contains?.(document.activeElement) ? document.activeElement : null;
    const row = active?.closest('[data-queued]')?.dataset.queued;
    // the control that had focus, by what it is: the summary, a queue action, or the Move to picker (with its picked value)
    const which = !active ? null : active.tagName === 'SUMMARY' ? 'summary' : active.dataset?.queueAction ? `[data-queue-action="${active.dataset.queueAction}"]`
        : active.tagName === 'SELECT' ? 'select' : null;
    const picked = active?.tagName === 'SELECT' ? active.value : null;
    box.replaceChildren(...(rows.length ? shown : [el('p', { class: 'row__note', text: 'Nothing is waiting.' })]));
    if (row && which) {   // the poll keeps the reader's place and an unsent pick
        const node = box.querySelector(`[data-queued="${CSS.escape(row)}"] ${which}`);
        if (node && picked !== null) node.value = picked;
        node?.focus({ preventScroll: true });
    }
}
