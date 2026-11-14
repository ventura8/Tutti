// where.js: the "Where Tutti runs" card (docs/MOVE.md). The host, its uptime, the clients connected through the bridge, the
// last move, and "Move Tutti to…": the plan first (a dry run), then the move, its progress step by step. During the move
// this dashboard stops; the page reconnects by itself and then shows the new host's dashboard through the client's
// tunnel, on the same address. Uses app.js's helpers (el, chip, $, relative, showBanner, TOKEN).
'use strict';

const whereUi = { plan: null, target: '', jobs: 'wait' };

async function movePost(body) {
    const res = await fetch('/api/move', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN }, body: JSON.stringify(body),
    });
    const answer = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
    return answer;
}

function since(seconds) {
    if (!seconds) return 'unknown';
    const s = Date.now() / 1000 - seconds;
    if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min`;
    if (s < 86400) return `${Math.round(s / 3600)} h`;
    return `${Math.round(s / 86400)} days`;
}

async function showPlan(button) {
    button.disabled = true;
    whereUi.plan = { text: 'Checking the target (ssh, disk, Tutti, addresses)…', blocked: true };
    renderWhere(ui.last);
    try {
        const a = await movePost({ action: 'plan', to: whereUi.target });
        whereUi.plan = { text: a.plan, blocked: a.blocked };
    } catch (err) {
        whereUi.plan = { text: `The plan could not be made: ${err.message}`, blocked: true };
    }
    button.disabled = false;
    renderWhere(ui.last);
}

async function startMove(action, button) {
    button.disabled = true;
    try {
        await movePost({ action, to: whereUi.target, jobs: whereUi.jobs });
        showBanner(el('strong', { text: action === 'move' ? `Moving Tutti to ${whereUi.target}. ` : 'Rolling back. ' }),
            'This page reconnects by itself; the card shows each step.');
    } catch (err) {
        showBanner(el('strong', { text: `${action} did not start. ` }), err.message);
        button.disabled = false;
    }
}

function movePicker(w) {
    if (!whereUi.target) whereUi.target = w.targets[0] || '';
    const pick = el('select', { 'aria-label': 'Move Tutti to', onchange: (e) => { whereUi.target = e.target.value; whereUi.plan = null; } },
        ...w.targets.map((t) => el('option', { value: t, text: t, selected: t === whereUi.target })));
    const jobs = el('select', { 'aria-label': 'Running jobs', onchange: (e) => { whereUi.jobs = e.target.value; } },
        el('option', { value: 'wait', text: 'wait for running jobs', selected: whereUi.jobs === 'wait' }),
        el('option', { value: 'handover', text: 'hand running jobs over', selected: whereUi.jobs === 'handover' }));
    const kids = [el('span', { text: 'Move Tutti to ' }), pick, jobs,
        el('button', { class: 'btn btn--tonal', type: 'button', text: 'Show the plan', onclick: (e) => showPlan(e.target) })];
    if (whereUi.plan && !whereUi.plan.blocked) {
        kids.push(el('button', { class: 'btn btn--filled', type: 'button', text: `Move to ${whereUi.target}`,
            onclick: (e) => startMove('move', e.target) }));
    }
    return el('div', { class: 'row__stats' }, ...kids);
}

function moveProgress(m) {
    const mark = (s) => {
        if (m.done.includes(s)) return ' ✓';
        if (s !== m.step) return '';
        return m.status === 'running' ? ' …' : ' ✗';
    };
    const steps = el('ol', { class: 'row__note' }, ...m.steps.map((s) => el('li', { text: s + mark(s) })));
    return el('li', { class: 'row' },
        el('div', { class: 'row__name', text: `Moving to ${m.to}` }),
        chip(m.status === 'running' ? `step ${m.done.length + 1} of ${m.steps.length}` : 'stopped', m.status === 'running' ? 'accent' : 'bad'),
        steps,
        m.error ? el('p', { class: 'row__note', text: `${m.error}. Continue with tutti move --to ${m.to}, or undo it.` }) : null,
        el('pre', { class: 'row__note', text: m.log.join('\n') }),
        m.status === 'stopped' ? el('button', { class: 'btn btn--tonal', type: 'button', text: 'Roll back',
            onclick: (e) => startMove('rollback', e.target) }) : null);
}

function renderWhere(s) {
    const w = s?.where;
    if (!w || !$('where')) return;
    const role = w.role === 'host' ? 'on this computer' : `on ${w.machine}, through this computer's client`;
    $('where-meta').textContent = `${w.machine} (${w.hostname}) · up ${since(w.started)} · ${role}`;
    const rows = [];
    if (w.readonly) rows.push(el('li', { class: 'row' }, chip('read-only', 'warn'), el('p', { class: 'row__note', text: w.readonly })));
    rows.push(el('li', { class: 'row' },
        el('div', { class: 'row__name', text: `Dashboard on 127.0.0.1:${w.port}` }),
        chip(w.address, 'idle'),
        el('p', { class: 'row__note', text: w.jobs ? `Worker jobs run on ${w.jobs} (its repositories and sign-ins), through its bridge.`
            : 'Worker jobs run here.' })));
    for (const c of w.clients) {
        rows.push(el('li', { class: 'row' }, el('div', { class: 'row__name', text: c.machine }), chip('client connected', 'ok'),
            el('p', { class: 'row__note', text: `Tutti ${c.version || '?'}, connected ${since(c.since)} ago` })));
    }
    if (w.move) rows.push(moveProgress(w.move));
    if (w.last) {
        const why = w.last.error ? `: ${w.last.error}` : '';
        rows.push(el('li', { class: 'row' }, el('div', { class: 'row__name', text: 'Last move' }),
            chip(w.last.result, w.last.result === 'moved' ? 'ok' : 'warn'),
            el('p', { class: 'row__note', text: `${w.last.from} → ${w.last.to}, ${since(w.last.at)} ago${why}` })));
    }
    if (w.role === 'host' && !w.move && w.targets.length) rows.push(el('li', { class: 'row' }, movePicker(w)));
    if (whereUi.plan) rows.push(el('li', { class: 'row' }, el('pre', { class: 'row__note', text: whereUi.plan.text })));
    $('where').replaceChildren(...rows);
}
