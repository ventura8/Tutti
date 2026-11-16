// outside.js: the "Work outside Tutti" card (docs/INTAKE.md). Commits in the autopilot repos and machine or device leases of
// the last 24 h that no Tutti task explains, so a session that works around Tutti is visible. Uses app.js's el, chip and $.
'use strict';

function renderOutside(s) {
    const o = s?.outside;
    if (!o || !$('outside')) return;
    const n = o.commits.length + o.leases.length;
    $('outside-meta').textContent = n ? `${n} in the last ${o.hours} h` : `none in the last ${o.hours} h`;
    const rows = o.commits.slice(0, 20).map((c) => el('li', { class: 'row' },
        el('div', { class: 'row__name', text: `${c.repo} ${c.sha}` }), chip('no task', 'warn'),
        el('p', { class: 'row__note', text: `${c.author}: ${c.subject}. Fix: claim a task (tutti backlog claim <id>) and name its id in the commit.` })));
    for (const l of o.leases) {
        rows.push(el('li', { class: 'row' }, el('div', { class: 'row__name', text: `${l.kind} ${l.name}` }), chip(l.why, 'warn'),
            el('p', { class: 'row__note', text: `Leased by ${l.who}. Fix: lease it for an open task (tutti lease claim … --task <id>).` })));
    }
    for (const r of o.unreadable) {
        rows.push(el('li', { class: 'row' }, el('div', { class: 'row__name', text: r }), chip('not read', 'warn'),
            el('p', { class: 'row__note', text: `Its git could not be read: check autopilot.repos.${r}.path in the config.` })));
    }
    if (!rows.length) {
        rows.push(el('li', { class: 'row' }, chip('all through Tutti', 'ok'),
            el('p', { class: 'row__note', text: 'Every commit and lease has its Tutti task.' })));
    }
    $('outside').replaceChildren(...rows);
}
