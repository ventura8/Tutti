// Tutti dashboard: what needs the owner, most urgent first, and how much of a long list to show (pure; app.js and a node
// test share it). Each item: {kind, tone, title, note, job?, task?}; job: a job id whose log it opens; task: an autopilot task
// whose attempts and failure history it shows.
const TUTTI_ATTENTION_ORDER = ['needs-person', 'panic', 'stalled', 'offline', 'down', 'reading', 'review', 'outside'];

function tuttiAttention(s) {
    const items = [];
    const ap = s.autopilot || {};
    for (const t of ap.parked || []) {
        items.push({ kind: 'needs-person', tone: 'bad', title: t.title, note: `${t.id} · ${t.repo}: ${t.parked || 'parked after its attempts'}`, task: t });
    }
    for (const a of s.usage?.accounts || []) {
        if (a.panic) {
            const limit = (a.limits || []).find(l => l.name === a.bound_by);
            const usage = limit ? `${limit.name}: ${tuttiPct(limit.used)} used` : `${tuttiPct(a.used)} used`;
            items.push({ kind: 'panic', tone: 'bad', title: `${a.name} is in panic (${usage})`, note: `Do: ${a.action}` });
        }
        else if (a.reader_problem) items.push({ kind: 'reading', tone: 'warn', title: `${a.name}: automatic reading failed`, note: a.reader_problem });
        else if (a.stale) items.push({ kind: 'reading', tone: 'warn', title: `${a.name} needs a fresh reading`, note: a.remind || 'Add a reading on its card.' });
    }
    for (const j of s.jobs?.items || []) {
        if (j.status === 'stalled') items.push({ kind: 'stalled', tone: 'warn', title: `Stalled: ${j.title}`, note: 'Its log has not changed for a while: open it, then stop or restart the job.', job: j.id });
    }
    for (const m of s.summary?.machines_offline || []) {
        items.push({ kind: 'offline', tone: 'warn', title: `${m} is offline`, note: 'Runs routed to it wait or go elsewhere: check its power and ssh.' });
    }
    for (const w of s.workers?.items || []) {
        if (!w.ready) items.push({ kind: 'down', tone: 'warn', title: `Worker ${w.name} is down`, note: w.note || 'See Workers.' });
    }
    for (const t of ap.review || []) {
        items.push({ kind: 'review', tone: 'ok', title: t.title, note: `${t.id} · ${t.repo}: ${t.publish_note || `branch ${t.branch}`}`, task: t });
    }
    if (s.summary?.outside) {
        items.push({ kind: 'outside', tone: 'warn', title: `${s.summary.outside} agent(s) started outside Tutti`, note: 'Their usage is not paced: start them with tutti run auto.' });
    }
    return items.sort((a, b) => TUTTI_ATTENTION_ORDER.indexOf(a.kind) - TUTTI_ATTENTION_ORDER.indexOf(b.kind));
}

// The planner's rows on an account card: [{label, text, title}]. A time is shown in the viewer's own time zone (`fmt`, the
// page's formatter) with UTC in its tooltip; a row without a value is left out.
function tuttiPlanRows(plan, fmt) {
    const utc = (iso) => `${new Date(iso).toUTCString().replace(/:\d\d GMT$/, '').replace(/^(\w+),/, '$1')} UTC`;
    return ((plan && plan.rows) || []).filter((r) => r.value || r.at).map((r) => (r.at
        ? { label: r.label, text: fmt(r.at), title: utc(r.at) }
        : { label: r.label, text: typeof tuttiTidy === 'function' ? tuttiTidy(String(r.value)) : String(r.value), title: '' }));
}

// A long list shows `limit` items and a "Show N more" button; opened, all of them and "Show fewer".
// How many rows of Running jobs and of Queued fit beside the main-agent and workers column: the column's height less what
// the jobs stack holds besides the two lists, shared by the two lists in turn (running first), at the rows' measured
// height. Columns stacked (a phone): the cap. At least `min` rows each, so a short column never hides everything.
function tuttiFit({ column, others, rowRunning, rowQueued, running, queued, phone, cap = 8, min = 3 }) {
    if (phone || !column) return { running: cap, queued: cap };
    let space = Math.max(0, column - others);
    const r = Math.max(min, Math.min(running, Math.floor(space / Math.max(1, rowRunning))));
    space -= Math.min(r, running) * rowRunning;
    const q = Math.max(min, Math.floor(space / Math.max(1, rowQueued)));
    return { running: r, queued: q };
}

function tuttiMore(count, limit, expanded) {
    if (count <= limit) return { shown: count, hidden: 0, button: '' };
    if (expanded) return { shown: count, hidden: 0, button: 'Show fewer' };
    return { shown: limit, hidden: count - limit, button: `Show ${count - limit} more` };
}

// What the owner opened or closed, kept across refreshes and reloads of the tab (app.js keeps it in sessionStorage).
// ui: {open, expanded, collapsed: Sets of ids; filter, project: strings}. A fold's `byDefault` holds until the owner toggles it.
const tuttiKept = {
    isOpen(ui, key, byDefault) {
        return ui.expanded.has(key) || (Boolean(byDefault) && !ui.collapsed.has(key));
    },
    toggled(ui, key, open) {
        (open ? ui.expanded : ui.collapsed).add(key);
        (open ? ui.collapsed : ui.expanded).delete(key);
    },
    dump(ui) {
        return JSON.stringify({ open: [...ui.open], expanded: [...ui.expanded], collapsed: [...ui.collapsed], filter: ui.filter, project: ui.project });
    },
    load(ui, text) {
        let kept = null;
        try { kept = JSON.parse(text || 'null'); } catch { kept = null; }   // a damaged entry: start from the defaults
        if (!kept || typeof kept !== 'object') return ui;
        for (const k of ['open', 'expanded', 'collapsed']) if (Array.isArray(kept[k])) ui[k] = new Set(kept[k].map(String));
        for (const k of ['filter', 'project']) if (typeof kept[k] === 'string') ui[k] = kept[k];
        return ui;
    },
};
