// Tutti dashboard: the Agents tab (#agents, t252, tutti/agentsview.py). One card per agent from /api/state's `agents`: its
// roles (from the config, each with why), what it can do, its limits and what it does now. Uses app.js's el() and chip().

const AGENT_TONE = { ready: 'ok', 'not ready': 'bad', checking: 'idle', paced: 'idle' };

// One agent's card (pure but for el(): node tests build it with a fake el)
function agentCard(a) {
    const list = (label, items) => (items.length ? [el('h4', { class: 'agent__head', text: label }),
        el('ul', { class: 'agent__list' }, ...items.map((x) => (typeof x === 'string' ? el('li', { text: x })
            : el('li', {}, el('strong', { text: x.label }), ` · ${x.why}`))))] : []);
    const where = [a.kind, a.model, a.machine ? `on ${a.machine}` : '', a.account && a.account !== a.name ? `account ${a.account}` : '']
        .filter(Boolean).join(' · ');
    return el('article', { class: 'card agent', 'data-agent': a.name, 'aria-labelledby': `agent-${a.name}` },
        el('header', { class: 'agent__top' },
            el('h3', { class: 'agent__name', id: `agent-${a.name}`, text: a.name }),
            chip(a.state, AGENT_TONE[a.state] || 'idle')),
        el('p', { class: 'row__note', text: where }),
        a.note ? el('p', { class: 'row__note', text: a.note }) : null,
        a.running ? el('p', { class: 'agent__now', text: `${a.running} running now` }) : null,
        ...list('Roles', a.roles), ...list('Can', a.capabilities), ...list('Limits', a.limits),
        a.roles.length ? null : el('p', { class: 'row__note', text: 'No role in the config yet: add it to routing.workers, main.fallback or review to use it.' }));
}

function renderAgents(s) {
    const grid = document.getElementById('agents-grid');
    if (!grid || !Array.isArray(s.agents)) return;
    grid.replaceChildren(...(s.agents.length ? s.agents.map(agentCard) : [el('p', { class: 'row__note', text: 'No agent is configured yet: tutti setup finds the ones on this computer.' })]));
}
