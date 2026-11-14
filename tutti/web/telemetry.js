// Tutti dashboard: telemetry. Script errors and unhandled rejections go to the local server (POST /api/telemetry), which
// scrubs them and records them on this computer; they leave it only when sending is on. Clicks on buttons and pickers are
// counted by panel and control id (never their text or values). Also the Insights card, the Self-improvement lane and the
// two switches (sending, self-improvement). Loaded first, so an error in app.js is caught too.
'use strict';

const TELEMETRY_TOKEN = document.querySelector('meta[name="tutti-token"]')?.content || '';
const telemetrySent = { errors: 0 };

function telemetryPost(body) {
    return fetch('/api/telemetry', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TELEMETRY_TOKEN }, body: JSON.stringify(body),
    }).catch(() => null);
}

function reportError(kind, err, message) {
    if (telemetrySent.errors >= 20) return;   // one broken loop never floods the server
    telemetrySent.errors += 1;
    void telemetryPost({ error: {
        kind, type: err?.name || 'Error',
        message: String(err?.message || message || '').slice(0, 500),
        stack: String(err?.stack || '').slice(0, 2500),
    } });
}

window.addEventListener('error', (e) => reportError('error', e.error, e.message));
window.addEventListener('unhandledrejection', (e) => reportError('unhandledrejection', e.reason, String(e.reason)));

// Which panel and which control: the card's heading id (usage-h -> usage) and the control's id, never what it shows.
function panelOf(target) {
    const card = target.closest('section.card, header.appbar, section.tiles');
    if (!card) return 'page';
    if (card.classList.contains('appbar')) return 'appbar';
    const head = card.querySelector('h2')?.id;
    return head ? head.replace(/-h$/, '') : card.id || 'page';
}

document.addEventListener('click', (e) => {
    const target = e.target.closest('button, select, a');
    if (!target) return;
    const control = (target.id || target.dataset.control || target.tagName.toLowerCase()).slice(0, 60);
    void telemetryPost({ use: { panel: panelOf(target), control } });
}, { capture: true });

async function telemetrySwitch(path, body, failText) {
    try {
        const res = await fetch(path, {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TELEMETRY_TOKEN }, body: JSON.stringify(body),
        });
        const answer = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
        await refresh();
    } catch (err) {
        showBanner(el('strong', { text: `${failText} ` }), err.message);
    }
}

function insightRows(title, rows, fmt) {
    return el('section', { class: 'autopilot__group' }, el('h3', { class: 'card__sub', text: title }),
        rows.length ? el('ul', { class: 'list' }, ...rows.map((r) => el('li', { class: 'row__note', text: fmt(r) })))
            : el('p', { class: 'row__note', text: 'Nothing yet.' }));
}

function renderInsights(t) {
    const i = t.insights;
    $('insights-meta').textContent = `last ${i.days} days · ${i.events} events · ${i.commands} commands`;
    $('insights').replaceChildren(
        insightRows('Most used', i.most_used, (r) => `${r.name}: ${r.runs}`),
        insightRows('Slowest (median)', i.slowest, (r) => `${r.name}: ${r.median_s} s (p90 ${r.p90_s} s)`),
        insightRows('Most failing', i.most_failing, (r) => `${r.name}: ${r.failed} of ${r.runs}`),
        insightRows('Wasted quota', i.wasted, (r) => `${r.who}: ${r.runs} failed runs, ${r.minutes} min`),
        insightRows('Stall causes', i.stalls, (r) => `${r.cause}: ${r.count}`),
        insightRows('Crashes', i.crashes, (r) => `${r.where}: ${r.count}`));
}

function lanePhase(item) {
    if (item.released) return ['released', 'ok'];
    if (item.merged) return ['merged', 'ok'];
    if (item.pr) return ['pull request', 'accent'];
    return [item.status, item.status === 'needs-person' ? 'warn' : 'idle'];
}

function laneKind(t) {
    const ref = t.ref ? ' ' + t.ref : '';
    return `${t.source}${ref} · ${t.project || ''}`;
}

function renderLane(si) {
    const toggle = $('selfimprove-toggle');
    toggle.textContent = si.enabled ? 'Turn off' : 'Turn on';
    toggle.setAttribute('aria-pressed', String(si.enabled));
    $('selfimprove-meta').textContent = `${si.enabled ? 'on' : 'off'} · ${si.items.length} task(s) from Sentry and the insights`;
    $('selfimprove').replaceChildren(...(si.items.length ? si.items.map((t) => {
        const [phase, tone] = lanePhase(t);
        return el('li', { class: 'row' },
            el('div', { class: 'row__name' }, `${t.id} ${t.title}`, el('span', { class: 'row__kind', text: laneKind(t) })),
            chip(phase, tone),
            t.pr ? el('a', { href: t.pr, target: '_blank', rel: 'noopener noreferrer', text: 'pull request' }) : null,
            t.gate ? el('p', { class: 'row__note', text: t.gate }) : null);
    }) : [el('li', {}, empty('Nothing to improve yet', 'Crashes from Sentry (through a scout) and the local insights become tasks here: issue, task, pull request, merge, release.'))]));
}

function renderSending(st) {
    const on = st.send === true;
    const toggle = $('telemetry-toggle');
    toggle.textContent = on ? 'Stop sending' : 'Send reports';
    toggle.setAttribute('aria-pressed', String(on));
    let target = 'nowhere yet: run tutti telemetry setup';
    if (st.endpoint_set) target = 'to your endpoint';
    if (st.dsn_set) target = st.dsn_from === 'release default' ? "to Tutti's maintainers (Sentry)" : 'to Sentry at ' + st.dsn_host;
    const problem = st.last_error ? ' Last send: ' + st.last_error : '';
    $('telemetry-state').textContent = `Crash reports and usage statistics: ${on ? 'sent' : 'not sent'} (${target}). `
        + `Recorded on this computer always; ${st.queued} queued.${problem}`;
}

function renderTelemetry(s) {
    if (!s.telemetry) return;
    renderConsent(s);   // report.js: the first-run question, until it is answered
    renderSending(s.telemetry.status);
    renderInsights(s.telemetry);
    renderLane(s.telemetry.self_improve);
}

document.addEventListener('DOMContentLoaded', () => {
    $('telemetry-toggle').addEventListener('click', () => {
        const on = $('telemetry-toggle').getAttribute('aria-pressed') !== 'true';
        void telemetrySwitch('/api/telemetry', { consent: on }, 'Sending was not switched.');
    });
    $('selfimprove-toggle').addEventListener('click', () => {
        const on = $('selfimprove-toggle').getAttribute('aria-pressed') !== 'true';
        void telemetrySwitch('/api/self-improve', { enabled: on }, 'Self-improvement was not switched.');
    });
});
