// Tutti dashboard: roles of machines and devices (tutti/roles.py, docs/MACHINES.md *Roles*): one chip per role whose tooltip says
// why (detected, from the config, or set by an admin), a Role filter per list, and the media devices found on the network.
'use strict';

const ROLE_SOURCE = { detected: 'detected', config: 'from the config', set: 'set by an admin' };

function roleNames(rows) {
    return (rows || []).map((r) => r.role).join(' ');
}

function roleChips(name, rows, settingsKey) {
    if (!(rows || []).length) return null;
    return el('ul', { class: 'roles', 'aria-label': `${name}: roles` }, ...rows.map((r) => el('li', {},
        el('a', { class: 'chip chip--role', 'data-source': r.source, href: `#settings/${settingsKey}.roles_add`,
            title: `${r.why} (${ROLE_SOURCE[r.source] || r.source}). Change it in Settings or with tutti roles set|unset.`, text: r.role }))));
}

// The filter keeps the chosen role across re-renders; a row without it is hidden.
function roleFilter(selectId, listId, items) {
    const sel = $(selectId);
    if (!sel) return;
    const all = [...new Set(items.flatMap((x) => (x.roles || []).map((r) => r.role)))].sort((a, b) => a.localeCompare(b));
    const chosen = all.includes(sel.value) ? sel.value : '';
    sel.replaceChildren(el('option', { value: '', text: 'All' }), ...all.map((r) => el('option', { value: r, text: r })));
    sel.value = chosen;
    sel.onchange = () => roleFilter(selectId, listId, items);
    for (const id of [listId, listId === 'devices' ? 'lan-devices' : null].filter(Boolean)) {
        for (const li of $(id)?.children || []) li.hidden = Boolean(chosen) && li.dataset.roles !== undefined
            && ![li, ...li.querySelectorAll('[data-roles]')].some((row) => row.dataset.roles.split(' ').includes(chosen));
    }
}

function renderLanDevices(s) {
    const lan = s.devices?.lan || [];
    const list = $('lan-devices');
    if (!list) return;
    list.hidden = !lan.length;
    $('lan-devices-h').hidden = !lan.length;
    list.replaceChildren(...lan.map((d) => el('li', { class: 'row', 'data-roles': roleNames(d.roles) },
        el('div', { class: 'row__name' }, d.name, el('span', { class: 'row__kind', text: `${d.address} · found on the network, no tests run on it` })),
        roleChips(d.name, d.roles, `lan_devices.${d.address}`))));
}
