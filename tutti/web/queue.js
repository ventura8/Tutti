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

const DIFFICULTIES = ['easy', 'medium', 'hard'];
const RUNNERS = ['local', 'online', 'both'];

function queueClassify(it) {
    const diff = el('select', { class: 'pick', 'aria-label': `Difficulty for ${it.id}` },
        ...DIFFICULTIES.map((d) => el('option', { value: d, text: d, selected: d === it.difficulty })));
    const run = el('select', { class: 'pick', 'aria-label': `Runner for ${it.id}` },
        ...RUNNERS.map((r) => el('option', { value: r, text: r, selected: r === it.runner })));
    return [diff, run, el('button', { class: 'btn btn--text', type: 'button', text: 'Save',
        onclick: () => queueAction({ kind: it.kind, id: it.id, action: 'classify', difficulty: diff.value, runner: run.value },
            `${it.id} was not updated.`) })];
}

function queueItem(it, ladder) {
    const acts = (it.actions || []).filter((a) => a !== 'move' && a !== 'classify').map((a) => el('button',
        { class: 'btn btn--text', type: 'button', 'data-queue-action': a, text: QUEUE_ACTIONS[a],
            onclick: () => queueAction({ kind: it.kind, id: it.id, action: a }, `${QUEUE_ACTIONS[a]} did not work for ${it.id}.`) }));
    if ((it.actions || []).includes('move') && ladder.length) acts.push(...queueMove(it, ladder));
    if ((it.actions || []).includes('classify')) acts.push(...queueClassify(it));
    const cls = it.difficulty ? ` · ${it.difficulty} · ${it.runner}` : '';
    return el('div', { class: 'row', 'data-queued': it.id, 'data-kind': it.kind },
        el('div', { class: 'row__name', text: `${it.title}` }),
        el('p', { class: 'row__note', text: `${it.kind} ${it.id} · size ${it.size}${cls} · goes to ${it.to} · starts ${it.eta}` }),
        el('p', { class: 'row__note', 'data-why': '', text: `Waiting: ${it.why}` }),
        acts.length ? el('div', { class: 'row__stats' }, ...acts) : '');
}

function renderQueue(s) {
    const q = s.queue;
    if (!q || !$('queued')) return;
    $('queued-meta').textContent = q.header;
    const ladder = s.autopilot?.ladder || [];
    // as many rows as fit beside the main-agent and workers column (app.js measureFit), then "Show more" for the rest
    const rows = q.groups.flatMap((g) => [
        el('h3', { class: 'card__sub queue__group', 'aria-label': `${g.project} (priority ${g.priority})`,
            text: `${g.project} · priority ${g.priority} · ${g.items.length} queued` }),
        ...g.items.map((it) => queueItem(it, q.can_act ? ladder : []))]);
    const limit = (typeof ui === 'object' && ui.fit?.queued) || 8;
    const shown = typeof more === 'function' ? more('queued', rows, limit, 'div') : rows;
    $('queued').replaceChildren(...(rows.length ? shown : [el('p', { class: 'row__note', text: 'Nothing is waiting.' })]));
}
