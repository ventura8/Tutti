'use strict';

const AGENT_FIELDS = {
    roles: 'Roles',
    capabilities: 'Capabilities',
    limits: 'Limits',
    live_state: 'Live state',
    learned: 'Learned strengths and limits',
    benchmarks: 'Benchmark scores',
    cost: 'Cost',
};

function agentText(value) {
    if (Array.isArray(value)) return value.map(agentText).join(', ') || 'Unavailable';
    if (value && typeof value === 'object') {
        return Object.entries(value).map(([key, item]) => `${key} — ${agentText(item)}`).join(', ') || 'Unavailable';
    }
    return value == null || value === '' ? 'Unavailable' : agentScalar(value);
}

function agentScalar(value) {
    return typeof value === 'number' ? tuttiNumberFormat({ maximumFractionDigits: 3 }).format(value) : String(value);
}

function agentActions(agent, title) {
    if (document.body.dataset.agentAdmin !== 'true') return null;
    const links = Object.entries(agent.actions || {}).map(([label, key]) =>
        el('a', { class: 'btn btn--tonal', href: `#settings/${encodeURIComponent(key)}`, text: label,
            'aria-label': `${label} · ${title}` }));
    if (agent.worker) {
        links.push(el('button', { class: 'btn btn--tonal', type: 'button', text: 'Set as main',
            'aria-label': `Set as main · ${title}`, onclick: () => setMainAgent(agent.worker) }));
    }
    return links.length ? el('div', { class: 'row__stats', 'data-admin': true }, links) : null;
}

function agentBenchmarks(rows) {
    if (!rows?.length) return 'Unavailable';
    return el('ul', {}, rows.map((row) =>
        el('li', {}, el('a', { class: 'agent-benchmark', href: row.source,
            text: `${row.name || 'Benchmark'} · ${agentScalar(row.score)}` }))));
}

function agentCard(agent) {
    const title = [agent.name, agent.machine || agent.account].filter(Boolean).join(' · ');
    const facts = Object.entries(AGENT_FIELDS).map(([field, label]) => {
        const value = field === 'benchmarks' ? agentBenchmarks(agent[field]) : agentText(agent[field]);
        return el('div', {}, el('dt', { text: label }), el('dd', {}, value));
    });
    return el('li', { class: 'card agent-card' },
        el('h3', { text: title }), el('dl', {}, facts), agentActions(agent, title));
}

function renderAgents(state) {
    const list = $('agents-list');
    if (!list || list.contains(document.activeElement)) return;
    const agents = state.agents || [];
    list.replaceChildren(...(agents.length ? agents.map(agentCard) : [el('li', { text: 'No agents configured.' })]));
}
