// Tutti dashboard: the Local models category of Usage & pacing (tutti/energy.py, docs/ENERGY.md). tuttiLocalCard is pure
// (a node test runs it); renderLocalModels draws it with app.js's el() and textContent only.
'use strict';

const TUTTI_LOCAL_TONE = { available: 'ok', paused: 'warn', yielding: 'warn', thermal: 'bad', offline: 'bad' };

function tuttiHours(seconds) {
    const s = Math.round(seconds || 0);
    if (s < 60) return `${s} s`;
    if (s < 3600) return `${Math.round(s / 60)} min`;
    return `${tuttiNum(s / 3600, { digits: 1 })} h`;
}

function tuttiMoney(v, currency) {
    if (v === null || v === undefined) return '–';
    return `${tuttiNum(v, { digits: v < 1 ? 2 : v < 100 ? 1 : 0 })} ${currency}`;
}

// One card as plain text rows: {title, gpu, status, tone, reason, models, power, periods[], perJob, perHour, cloud, measured}
function tuttiLocalCard(c, t) {
    const cur = t.currency;
    const loaded = (c.loaded || []).map((m) => `${m.name}${m.on_gpu ? '' : ' (CPU)'}`);
    const p = c.parts || {};
    const periods = [['Today', 'today'], ['Week', 'week'], ['Month', 'month']].map(([label, k]) => {
        const x = (c.periods || {})[k] || {};
        return { label, busy: tuttiHours(x.busy_s), jobs: tuttiNum(x.jobs || 0), tokens: tuttiNum(x.tokens || 0, { digits: 0 }), kwh: tuttiNum(x.kwh || 0, { digits: 2 }),
            cost: tuttiMoney(x.cost || 0, cur) };
    });
    const month = (c.periods || {}).month || {};
    const cloud = month.cloud && month.tokens
        ? `The same tokens this month on ${month.cloud.model} would cost about ${tuttiMoney(month.cloud.cost ?? month.cloud.usd, month.cloud.cost === null ? 'USD' : cur)}`
        : 'No tokens counted this month yet: the cloud comparison starts with the first local-model run';
    return {
        title: c.name,
        gpu: c.gpu,
        status: c.busy_now ? 'busy' : c.status,
        tone: c.busy_now ? 'accent' : (TUTTI_LOCAL_TONE[c.status] || 'idle'),
        reason: c.reason,
        models: [`Loaded: ${loaded.length ? loaded.join(', ') : 'none'}`, `Installed: ${(c.installed || []).length ? c.installed.join(', ') : 'not read'}`],
        power: c.ha_measured ? `${tuttiNum(c.watts, { digits: 0 })} W · Home Assistant` : `${c.gpu_measured ? '' : '≈ '}${tuttiNum(c.watts, { digits: 0 })} W now (GPU ${tuttiNum(p.gpu, { digits: 0 })} W ${c.gpu_measured ? 'measured' : 'estimated'}${c.util === null || c.util === undefined ? '' : ` at ${tuttiPct(c.util)}`} + CPU ${tuttiNum(p.cpu, { digits: 0 })} W ${(c.measured || []).some((m) => m.startsWith('CPU')) ? 'measured' : 'estimated'} + base ${tuttiNum(p.base, { digits: 0 })} W)`,
        measured: c.ha_measured ? 'Home Assistant' : c.measured && c.measured.length ? `Measured: ${c.measured.join(', ')}; the rest estimated` : 'Estimated: no power reading here (TDP × utilisation)',
        denied: (c.denied || []).map((d) => `To measure instead of estimate: ${d}`),
        periods,
        perJob: c.per_job === null || c.per_job === undefined ? 'no job this month' : tuttiMoney(c.per_job, cur),
        perHour: tuttiMoney(c.per_busy_hour, cur),
        cloud,
    };
}

function tuttiTariffLine(t) {
    const est = t.estimated ? 'estimated, typical for ' : '';
    return `Electricity ${t.price} ${t.currency}/kWh (${est}${t.country || 'unknown country'}, found from ${t.how})${t.periods?.length ? ' · time-of-day tariff' : ''}`;
}

// Each model's measured tier (tutti/power.py): easy tasks go to the weakest tier that can do them, hard ones to the strongest
function modelTiers(fleet, machine) {
    const models = (fleet?.workers || []).filter((w) => w.machine === machine).flatMap((w) => w.models || []);
    const seen = new Set();
    return models.filter((m) => m.rank_tier && !seen.has(m.name) && seen.add(m.name)).map((m) => el('p', { class: 'account__auto',
        text: `${m.name}: ${m.rank_tier} tier (${m.rank_why})${m.tokens_s ? ` · ${m.tokens_s} tok/s` : ''}` }));
}

function renderLocalModels(s) {
    const lm = s.local_models;
    const box = document.getElementById('local-models');
    if (!box || !lm) return;
    document.getElementById('local-meta').textContent = lm.error || `${tuttiTariffLine(lm.tariff)}${lm.cheap_wait ? ` · ${lm.cheap_wait}` : ''}`;
    if (!lm.cards.length) {
        box.replaceChildren(empty('No local models', codeText('Add an Ollama worker (kind: ollama) to the config (see ', 'tutti.config.example.yaml', ').')));
        return;
    }
    box.replaceChildren(...lm.cards.map((c) => {
        const v = tuttiLocalCard(c, lm.tariff);
        const table = el('div', { class: 'local__scroll', tabindex: '0', role: 'region', 'aria-label': `${v.title}: energy and cost by period` }, el('table', { class: 'local__periods' },
            el('thead', {}, el('tr', {}, el('th', { scope: 'col' }, el('span', { class: 'sr-only', text: 'Period' })),
                ...['Busy', 'Jobs', 'Tokens', 'kWh', 'Cost'].map((h) => el('th', { scope: 'col', text: h })))),
            el('tbody', {}, ...v.periods.map((r) => el('tr', {}, el('th', { scope: 'row', text: r.label }),
                el('td', { text: r.busy }), el('td', { text: String(r.jobs) }), el('td', { text: String(r.tokens) }),
                el('td', { text: r.kwh }), el('td', { text: r.cost }))))));
        const method = el('details', { class: 'local__method' }, el('summary', { text: 'Estimates: how they are made' }),
            el('p', { text: v.measured }), ...v.denied.map((d) => el('p', { text: d })),
            el('p', { text: `Energy is the power sampled at each probe while a Tutti run is on this machine${lm.include_idle ? ', plus the idle draw while Tutti keeps a model loaded' : ''}; cost is kWh × the price of that hour. ${lm.tariff.source}.` }),
            el('p', { text: `Cloud prices: ${(c.periods.month.cloud || {}).source || ''}. Method: ${lm.method}` }));
        return el('article', { class: 'account local', 'data-machine': c.name, 'aria-label': `${v.title}: ${v.status}` },
            el('div', { class: 'account__top' }, el('h3', { class: 'account__name', text: v.title }), chip(v.status, v.tone)),
            el('p', { class: 'account__by', text: v.gpu }),
            ...v.models.map((m) => el('p', { class: 'account__auto', text: m })),
            ...modelTiers(s.fleet, c.machine || c.name),
            el('p', { class: 'account__auto', text: v.power }),
            table,
            el('p', { class: 'account__auto', text: `Per job ${v.perJob} · per busy hour ${v.perHour}` }),
            el('p', { class: 'account__plan', text: v.cloud }),
            (v.status === 'available' || v.status === 'busy') && c.reachable ? null : el('p', { class: 'account__remind', 'data-tone': 'warn', role: 'note', text: v.reason }),
            method);
    }));
}
