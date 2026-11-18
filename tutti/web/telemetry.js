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

function improveTone(e) {
    const outcome = e.outcome || e.status || '';
    if (outcome === 'skipped_changed') return 'idle';
    if (outcome === 'kept' || e.status === 'kept') return 'ok';
    if (outcome === 'rolled_back' || e.status === 'rolled_back') return 'warn';
    return 'accent';
}

function improveRow(e) {
    const title = `${e.aspect || ''} ${e.path || ''}: ${(e.old != null ? e.old + ' → ' : '')}${e.value != null ? e.value : e.new != null ? e.new : ''}`;
    const note = [e.evidence, e.expected_gain].filter(Boolean).join(' · ');
    const label = e.outcome === 'skipped_changed' ? 'skipped_changed' : (e.status || e.outcome || 'proposed');
    return el('li', { class: 'row' },
        el('div', { class: 'row__name' }, title, note ? el('span', { class: 'row__kind', text: note }) : null),
        chip(label, improveTone(e)));
}

function renderImprovements(im) {
    if (!im) return;
    const toggle = $('improvements-toggle');
    toggle.textContent = im.enabled ? 'Turn off' : 'Turn on';
    toggle.setAttribute('aria-pressed', String(im.enabled));
    $('improvements-meta').textContent = im.enabled
        ? `${im.autonomy} · ${im.waiting} waiting · ${im.applied} applied · ${im.kept} kept · ${im.rolled_back} rolled back`
            + (im.skipped_changed ? ` · ${im.skipped_changed} rollback skipped` : '')
            + (im.share ? ' · sharing kept reports' : '')
        : 'off';
    if (!im.enabled) {
        $('improvements').replaceChildren(empty('Improvements is off',
            'Turned on, Tutti reviews telemetry and proposes changes across routing, pacing and parallel limits; small ones apply within admin autonomy, then are measured and kept or rolled back.'));
        return;
    }
    const waiting = (im.proposals || []).map((p) => el('li', { class: 'row' },
        el('div', { class: 'row__name' }, `${p.aspect} ${p.path}: ${p.old} → ${p.value}`,
            el('span', { class: 'row__kind', text: `${p.evidence || ''} · gain: ${p.expected_gain || ''}` })),
        el('button', { class: 'btn btn--tonal', type: 'button', 'data-control': 'improve-apply',
            onclick: () => void telemetrySwitch('/api/improvements', { proposal: p.path, apply: true },
                'The improvement was not applied.') }, 'Apply'),
        el('button', { class: 'btn', type: 'button', 'data-control': 'improve-dismiss',
            onclick: () => void telemetrySwitch('/api/improvements', { proposal: p.path, apply: false },
                'The proposal was not dismissed.') }, 'Dismiss')));
    $('improvements').replaceChildren(
        el('p', { class: 'row__note', text: `Aspects: ${(im.aspects || []).join(', ')}. Share kept reports: ${im.share ? 'on' : 'off'} (admin opt-in).` }),
        el('button', { class: 'btn', type: 'button', id: 'improvements-share', 'aria-pressed': String(!!im.share),
            onclick: () => void telemetrySwitch('/api/improvements', { share: !im.share }, 'Sharing was not switched.') },
        im.share ? 'Stop sharing kept reports' : 'Share kept reports'),
        el('section', { class: 'autopilot__group' }, el('h3', { class: 'card__sub', text: 'Waiting for you' }),
            waiting.length ? el('ul', { class: 'list' }, ...waiting) : el('p', { class: 'row__note', text: 'Nothing waits for you.' })),
        el('section', { class: 'autopilot__group' }, el('h3', { class: 'card__sub', text: 'Log' }),
            (im.log || []).length ? el('ul', { class: 'list' }, ...im.log.map(improveRow))
                : el('p', { class: 'row__note', text: 'Nothing yet.' })));
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

function aiSwitch(name, on) {
    return el('button', { class: 'btn', type: 'button', id: `aiassist-${name}`, 'aria-pressed': String(on),
        onclick: () => void telemetrySwitch('/api/ai-assist', { switch: name, on: !on }, 'AI assist was not switched.') },
    `${name.replace('_', ' ')}: ${on ? 'on' : 'off'}`);
}

function renderAiAssist(a) {
    if (!a) return;
    const on = a.switches.enabled;
    const toggle = $('aiassist-toggle');
    toggle.textContent = on ? 'Turn off' : 'Turn on';
    toggle.setAttribute('aria-pressed', String(on));
    $('aiassist-meta').textContent = on
        ? `${a.proposals} proposal(s) · ${a.done} done · ${a.tuned.filter((t) => !t.undone).length} parameter(s) tuned`
        : 'off: an admin opts in';
    if (!on) {
        $('aiassist').replaceChildren(empty('AI assist is off', 'Turned on, your local models read Tutti\'s telemetry and failures and propose fixes, retries are smarter, small parameter changes tune themselves, and the scout researches new models and prices with spare quota only.'));
        return;
    }
    const tuned = a.tuned.map((t) => el('li', { class: 'row' },
        el('div', { class: 'row__name' }, `${t.path}: ${t.old} → ${t.new}`, el('span', { class: 'row__kind', text: t.why })),
        t.undone ? chip('undone', 'idle') : el('button', { class: 'btn', type: 'button', 'data-control': 'aiassist-undo',
            onclick: () => void telemetrySwitch('/api/ai-assist', { undo: t.id }, 'The change was not undone.') }, 'Undo')));
    const brain = a.brain || { line: '', parks: [] };
    const ub = a.unblocker || { recent: [] };
    $('aiassist').replaceChildren(
        el('p', { class: 'row__name', 'data-field': 'brain-line', text: brain.line }),
        el('p', { class: 'row__note', text: `This week: ${brain.local || 0} local, ${brain.online || 0} online, ${brain.wh || 0} Wh` }),
        insightRows('Parked for Tutti\'s own job', brain.parks || [], (p) => p),
        insightRows('Unblocker', ub.recent, (e) => `${e.id} (${e.kind}, ${e.by}): ${e.outcome}`),
        el('p', { class: 'row__note' }, ...['local', 'online', 'online_code', 'auto_tune'].map((k) => aiSwitch(k, a.switches[k]))),
        insightRows('Measured gains', a.gains, (g) => g),
        insightRows('Latest proposals', a.recent, (p) => `${p.title} (${p.by})`),
        el('section', { class: 'autopilot__group' }, el('h3', { class: 'card__sub', text: 'Parameters tuned' }),
            tuned.length ? el('ul', { class: 'list' }, ...tuned) : el('p', { class: 'row__note', text: 'Nothing yet.' })),
        el('section', { class: 'autopilot__group' }, el('h3', { class: 'card__sub', text: 'Waiting for you (larger changes)' }),
            a.tune_proposals.length ? el('ul', { class: 'list' }, ...a.tune_proposals.map((t) => el('li', { class: 'row' },
                el('div', { class: 'row__name' }, `${t.path}: ${t.old} → ${t.value}`, el('span', { class: 'row__kind', text: t.why })),
                el('button', { class: 'btn btn--tonal', type: 'button', 'data-control': 'aiassist-apply',
                    onclick: () => void telemetrySwitch('/api/ai-assist', { proposal: t.path, apply: true }, 'The change was not applied.') }, 'Apply'),
                el('button', { class: 'btn', type: 'button', 'data-control': 'aiassist-dismiss',
                    onclick: () => void telemetrySwitch('/api/ai-assist', { proposal: t.path, apply: false }, 'The proposal was not dismissed.') }, 'Dismiss'))))
                : el('p', { class: 'row__note', text: 'Nothing waits for you.' })),
        ...(a.skipped ? [el('p', { class: 'row__note', text: a.skipped })] : []));
}

function renderTelemetry(s) {
    if (!s.telemetry) return;
    renderConsent(s);   // report.js: the first-run question, until it is answered
    renderSending(s.telemetry.status);
    renderInsights(s.telemetry);
    renderLane(s.telemetry.self_improve);
    renderImprovements(s.telemetry.improvements);
    renderAiAssist(s.telemetry.ai_assist);
}

document.addEventListener('DOMContentLoaded', () => {
    $('telemetry-toggle').addEventListener('click', () => {
        const on = $('telemetry-toggle').getAttribute('aria-pressed') !== 'true';
        void telemetrySwitch('/api/telemetry', { consent: on }, 'Sending was not switched.');
    });
    $('aiassist-toggle').addEventListener('click', () => {
        const on = $('aiassist-toggle').getAttribute('aria-pressed') !== 'true';
        void telemetrySwitch('/api/ai-assist', { switch: 'enabled', on }, 'AI assist was not switched.');
    });
    $('selfimprove-toggle').addEventListener('click', () => {
        const on = $('selfimprove-toggle').getAttribute('aria-pressed') !== 'true';
        void telemetrySwitch('/api/self-improve', { enabled: on }, 'Self-improvement was not switched.');
    });
    $('improvements-toggle').addEventListener('click', () => {
        const on = $('improvements-toggle').getAttribute('aria-pressed') !== 'true';
        void telemetrySwitch('/api/improvements', { enabled: on }, 'Improvements was not switched.');
    });
});
