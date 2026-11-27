// Tutti dashboard: the Queued list beside Running jobs (tutti/waitlist.py). Everything waiting, grouped by project in
// priority order, each with its size, where it goes, its estimated start and why it waits; an admin's actions (Run now,
// Move to, Cancel, Priority up or down) post to /api/queue. Uses app.js's helpers (el, $, chip, refresh, showBanner, TOKEN).

const QUEUE_ACTIONS = { run: 'Run now', up: 'Priority up', down: 'Priority down', cancel: 'Cancel', claim: 'Claim', retry: 'Retry' };

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

function detailValue(label, value) {
    if (['PR', 'CI'].includes(label) && /^https?:\/\/[^\s]+$/i.test(value)) {
        return el('a', { href: value, class: 'fact-link', text: value });
    }
    return value;
}

function detailCi(facts) {
    const pr = facts.find(([label]) => label === 'PR')?.[1];
    if (!pr || facts.some(([label]) => label === 'CI')) return [];
    const projects = typeof ui === 'object' ? ui.last?.projects?.items || [] : [];
    const request = projects.flatMap((project) => project.prs?.items || []).find(({ url }) => url === pr);
    if (!request?.ci) return [];
    const link = el('a', { href: `${pr}/checks`, class: 'fact-link', text: request.ci, title: request.ci_detail });
    return [el('dt', { text: 'CI' }), el('dd', {}, link)];
}

function detailList(facts, id) {
    const entries = facts || [];
    const rows = entries.flatMap(([label, value, run], index) => {
        const target = run ? `${id}-log-${index}` : undefined;
        const name = run ? el('a', { href: `#${encodeURIComponent(target)}`, class: 'fact-link', text: label, onclick: (event) => {
            event.preventDefault();
            const jobId = `run:${run}.log`;
            if (ui.last?.jobs?.items?.some((job) => job.id === jobId)) openJob(jobId);
            else document.getElementById(target)?.focus();
        } }) : label;
        return [el('dt', {}, name), el('dd', { id: target, tabindex: run ? '-1' : undefined }, detailValue(label, value))];
    });
    return el('dl', { class: 'facts' }, ...rows, ...detailCi(entries));
}

function rowDisclosure(key, title, entries, attrs = {}, actions = null) {
    const open = typeof ui === 'object' && ui.expanded.has(key);
    const id = `facts-${encodeURIComponent(key)}`;
    const facts = el('div', { class: 'row__details', id, hidden: !open }, detailList(entries, id), actions);
    const button = el('button', { class: 'btn btn--text row__toggle view-ok', type: 'button', ...attrs,
        'aria-expanded': String(open), 'aria-controls': id, text: title,
        onclick: () => {
            const next = facts.hidden;
            tuttiKept.toggled(ui, key, next);
            saveUi();
            facts.hidden = !next;
            button.setAttribute('aria-expanded', String(next));
        } });
    return [button, facts];
}

function taskActions(it, ladder = []) {
    if (typeof document === 'object' && document.body.dataset.role === 'viewer') return null;
    const acts = (it.actions || []).filter((a) => a !== 'move').map((a) => el('button', { class: 'btn btn--text', type: 'button',
        'data-queue-action': a, text: QUEUE_ACTIONS[a],
        onclick: () => {
            const who = a === 'claim' ? window.prompt('Which session will work on this task?')?.trim() : undefined;
            if (a === 'claim' && !who) return;
            queueAction({ kind: it.kind || 'task', id: it.id, action: a, who }, `${QUEUE_ACTIONS[a]} did not work for ${it.id}.`);
        } }));
    if ((it.actions || []).includes('move') && ladder.length) acts.push(...queueMove(it, ladder));
    return acts.length ? el('div', { class: 'row__stats' }, ...acts) : null;
}

function queueItem(it, ladder) {
    const [button, facts] = rowDisclosure(`queued:${it.kind}:${it.project}:${it.id}`, it.title, it.facts,
        { 'data-queue-toggle': it.id }, taskActions(it, ladder));
    return el('div', { class: 'row', 'data-queued': it.id, 'data-kind': it.kind },
        el('div', { class: 'row__name' }, button),
        el('p', { class: 'row__note', text: `${it.kind} ${it.id} · size ${it.size} · goes to ${it.to} · starts ${it.eta}` }),
        el('p', { class: 'row__note', 'data-why': '', text: `Waiting: ${it.why}` }),
        facts);
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
    const replace = typeof replaceRows === 'function' ? replaceRows : (box, ...children) => box.replaceChildren(...children);
    replace($('queued'), ...(rows.length ? shown : [el('p', { class: 'row__note', text: 'Nothing is waiting.' })]));
}
