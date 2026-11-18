'use strict';

function renderAgents(s) {
    const list = $('agents-list');
    if (!list) return;
    list.replaceChildren(...(s.agents || []).map(agentCard));
}

function agentCard(a) {
    return el('li', { class: 'card' },
        el('div', { class: 'card__head' },
            el('h3', { text: a.name }),
            el('span', { class: 'chip', 'data-tone': a.ready ? 'ok' : 'bad', text: a.ready ? 'ready' : 'DOWN' })
        ),
        el('p', { class: 'row__note', text: `Kind: ${a.kind} · ${a.note}` }),
        el('div', { class: 'row__stats' },
            el('p', {}, el('strong', { text: 'Roles: ' }), (a.roles || []).join(', ')),
            el('p', {}, el('strong', { text: 'Capabilities: ' }), (a.capabilities || []).join(', ')),
            el('p', {}, el('strong', { text: 'Limits: ' }), JSON.stringify(a.limits || {})),
            el('p', {}, el('strong', { text: 'Live State: ' }), JSON.stringify(a.live_state || {})),
            el('p', {}, el('strong', { text: 'Learned: ' }), (a.learned || []).join(', ')),
            el('p', {}, el('strong', { text: 'Benchmarks: ' }), JSON.stringify(a.benchmarks || [])),
            el('p', {}, el('strong', { text: 'Cost: ' }), JSON.stringify(a.cost || {})),
            el('p', {}, el('strong', { text: 'Actions: ' }), (a.actions || []).join(', '))
        )
    );
}
