// Tutti dashboard: the Setup wizard (#setup; docs/SETUP-WIZARD.md). The same steps as `tutti setup --tui`, from one engine
// (tutti/wizard.py, GET and POST /api/wizard): welcome, main and backup machines, machines, accounts and agents, GitHub and
// repositories, Android devices, integrations, household, review. Each step is prefilled with what Tutti detected; Apply
// writes only what differs; a sign-in is a command to copy and run (Tutti never sees a credential). It opens by itself on
// the first run and from Settings at any time; a viewer sees the household-wide steps locked.
'use strict';

const WIZ_TOKEN = document.querySelector('meta[name="tutti-token"]')?.content || '';
const WIZ = { data: null, open: null, message: '', tone: '', busy: false };
const WIZ_TONE = { done: 'ok', 'needs-you': 'warn', optional: 'idle', skipped: 'idle', locked: 'idle' };
const WIZ_LABEL = { done: 'Done', 'needs-you': 'Needs you', optional: 'Optional', skipped: 'Skipped', locked: 'Admin only' };
const WIZ_ROW = { ok: ['ok', 'OK'], todo: ['warn', 'Needs you'], info: ['idle', 'Note'] };

function wizEl(tag, props, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
        if (v === undefined || v === null || v === false) continue;
        if (k === 'text') node.textContent = v;
        else if (k === 'class') node.className = v;
        else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
        else if (k === 'checked' || k === 'value') node[k] = v;
        else node.setAttribute(k, String(v));
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) node.append(kid);
    return node;
}

function wizChip(text, tone) {
    return wizEl('span', { class: 'chip', 'data-tone': tone, text });
}

function wizId(key) {
    return `wiz-${String(key).replace(/[^A-Za-z0-9_-]/g, '-')}`;
}

async function wizFetch(body) {
    const opts = body ? { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': WIZ_TOKEN }, body: JSON.stringify(body) }
        : { cache: 'no-store' };
    const res = await fetch('/api/wizard', opts);
    const answer = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
    return answer;
}

function wizCopy(command) {
    return wizEl('button', {
        class: 'btn btn--small', type: 'button', text: 'Copy', 'aria-label': `Copy: ${command}`,
        onclick: (e) => navigator.clipboard.writeText(command).then(() => { e.target.textContent = 'Copied'; }, () => {}),
    });
}

function wizRow(r) {
    const [tone, label] = WIZ_ROW[r.state] || WIZ_ROW.info;
    return wizEl('li', { class: 'row' },
        wizEl('div', { class: 'row__name', text: r.title }), wizChip(label, tone),
        wizEl('p', { class: 'row__note', text: r.detail }),
        r.command ? wizEl('p', { class: 'row__note wizard__cmd' }, wizEl('code', { text: r.command }), ' ', wizCopy(r.command)) : null);
}

function wizChoice(f, multi) {
    const box = wizEl('fieldset', { class: 'wizard__field' }, wizEl('legend', { text: f.label }));
    if (!f.options.length) box.append(wizEl('p', { class: 'row__note', text: 'Nothing to choose from yet.' }));
    f.options.forEach((o, i) => {
        const id = `${wizId(f.key)}-${i}`;
        const on = multi ? f.value.includes(o.value) : f.value === o.value;
        box.append(wizEl('div', { class: 'wizard__option' },
            wizEl('input', { type: multi ? 'checkbox' : 'radio', id, name: wizId(f.key), value: o.value, checked: on, 'data-key': f.key }),
            wizEl('label', { for: id }, wizEl('span', { text: o.label }), o.note ? wizEl('span', { class: 'row__note', text: ` ${o.note}` }) : null)));
    });
    return box;
}

function wizField(f) {
    if (f.kind === 'choice' || f.kind === 'multi') return wizChoice(f, f.kind === 'multi');
    const id = wizId(f.key);
    if (f.kind === 'bool') {
        return wizEl('div', { class: 'wizard__field wizard__option' },
            wizEl('input', { type: 'checkbox', id, checked: Boolean(f.value), 'data-key': f.key }),
            wizEl('label', { for: id }, wizEl('span', { text: f.label }), f.note ? wizEl('span', { class: 'row__note', text: ` ${f.note}` }) : null));
    }
    const input = wizEl('input', { id, class: 'setting__input', 'data-key': f.key, type: f.kind === 'number' ? 'number' : 'text',
        value: String(f.value ?? ''), min: f.min, max: f.max, placeholder: f.placeholder, autocomplete: 'off' });
    return wizEl('div', { class: 'wizard__field' }, wizEl('label', { for: id, text: f.label }), input);
}

function wizValues(form, step) {
    const out = {};
    for (const f of step.fields) {
        const inputs = [...form.querySelectorAll('[data-key]')].filter((i) => i.dataset.key === f.key);
        if (f.kind === 'multi') out[f.key] = inputs.filter((i) => i.checked).map((i) => i.value);
        else if (f.kind === 'choice') out[f.key] = (inputs.find((i) => i.checked) || {}).value ?? f.value;
        else if (f.kind === 'bool') out[f.key] = inputs[0].checked;
        else if (f.kind === 'number') out[f.key] = inputs[0].value === '' ? null : Number(inputs[0].value);
        else out[f.key] = inputs[0].value;
    }
    return out;
}

async function wizApply(step, data) {
    if (WIZ.busy) return;
    WIZ.busy = true;
    try {
        const answer = await wizFetch({ step: step.id, data });
        WIZ.data = answer;
        WIZ.message = answer.message || 'Saved.';
        WIZ.tone = 'ok';
        const i = answer.steps.findIndex((s) => s.id === step.id);
        if (!data.skip || i >= 0) WIZ.open = answer.steps[Math.min(i + 1, answer.steps.length - 1)].id;
    } catch (err) {
        WIZ.message = `Not applied: ${err.message}`;
        WIZ.tone = 'bad';
    } finally {
        WIZ.busy = false;
    }
    drawWizard(true);
}

function wizStepPanel(step) {
    const form = wizEl('form', { class: 'wizard__form', 'aria-labelledby': 'wizard-step-h', novalidate: true });
    form.append(...step.fields.map(wizField));
    const last = step.id === 'review';
    const actions = wizEl('div', { class: 'wizard__actions' });
    if (!step.locked) {
        actions.append(wizEl('button', { class: 'btn btn--filled', type: 'submit', id: 'wizard-apply',
            text: last ? 'Finish setup' : step.id === 'welcome' ? 'Start' : 'Apply and continue' }));
        if (step.id !== 'welcome' && !last) {
            actions.append(wizEl('button', { class: 'btn btn--text', type: 'button', id: 'wizard-skip', text: 'Skip for now',
                onclick: () => void wizApply(step, { skip: true }) }));
        }
    }
    form.append(actions);
    form.addEventListener('submit', (e) => {
        e.preventDefault();
        void wizApply(step, wizValues(form, step));
    });
    return wizEl('section', { class: 'card wizard__step', 'aria-labelledby': 'wizard-step-h' },
        wizEl('header', { class: 'card__head' },
            wizEl('h3', { id: 'wizard-step-h', tabindex: '-1', text: step.title }),
            wizChip(WIZ_LABEL[step.status] || step.status, WIZ_TONE[step.status] || 'idle')),
        step.locked ? wizEl('p', { class: 'row__note', text: 'Only an admin can change this step: it sets up the whole household.' }) : null,
        step.rows.length ? wizEl('ul', { class: 'list', 'aria-label': `${step.title}: what Tutti found` }, ...step.rows.map(wizRow)) : null,
        (step.links || []).length ? wizEl('p', { class: 'row__note' }, ...step.links.map((l) => wizEl('a', { class: 'link', href: l.href, text: `Open the ${l.label}` }))) : null,
        form);
}

function wizNav(steps) {
    return wizEl('ol', { class: 'wizard__steps', 'aria-label': 'Setup steps' }, ...steps.map((s, i) => wizEl('li', {},
        wizEl('button', { class: 'wizard__stepbtn view-ok', type: 'button',   // navigation only: a viewer may use it 'aria-current': s.id === WIZ.open ? 'step' : null,
            onclick: () => { WIZ.open = s.id; WIZ.message = ''; drawWizard(true); } },
        wizEl('span', { text: `${i + 1}. ${s.title}` }), wizChip(WIZ_LABEL[s.status] || s.status, WIZ_TONE[s.status] || 'idle')))));
}

function drawWizard(focus) {
    const box = document.getElementById('wizard-body');
    if (!box || !WIZ.data) return;
    const steps = WIZ.data.steps;
    if (!steps.some((s) => s.id === WIZ.open)) WIZ.open = WIZ.data.current;
    const step = steps.find((s) => s.id === WIZ.open);
    const done = steps.filter((s) => s.status === 'done').length;
    document.getElementById('wizard-meta').textContent = WIZ.data.finished ? `Finished; ${done} of ${steps.length} steps done`
        : `${done} of ${steps.length} steps done`;
    document.getElementById('wizard-detect').hidden = !WIZ.data.admin;
    const msg = document.getElementById('wizard-msg');
    msg.textContent = WIZ.message;
    msg.dataset.tone = WIZ.tone;
    box.replaceChildren(wizNav(steps), wizStepPanel(step));
    if (focus) document.getElementById('wizard-step-h')?.focus();
}

async function loadWizard() {
    try {
        WIZ.data = await wizFetch();
    } catch (err) {
        const box = document.getElementById('wizard-body');
        if (box) box.replaceChildren(wizEl('p', { class: 'setting__msg', 'data-tone': 'bad', role: 'alert',
            text: `The setup wizard did not load (${err.message}): reload to try again, or run tutti setup --tui in a terminal.` }));
        return null;
    }
    drawWizard(false);
    return WIZ.data;
}

function showWizardView() {
    const on = location.hash.startsWith('#setup');
    const view = document.getElementById('wizard-view');
    if (!view) return;
    view.hidden = !on;
    if (!on) return;
    for (const id of ['dashboard-view', 'settings-view', 'chat-view', 'users-view']) {
        const v = document.getElementById(id);
        if (v) v.hidden = true;
    }
    for (const a of document.querySelectorAll('.tabs__tab[data-view]')) a.removeAttribute('aria-current');
    void loadWizard();
}

async function wizardFirstRun() {
    // the first run: an admin who has never opened the wizard lands in it (once; later from Settings, Setup wizard)
    if (location.hash) return;
    const data = await loadWizard();
    if (data && data.first_run) location.hash = '#setup';
}

if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('DOMContentLoaded', () => {
        window.addEventListener('hashchange', showWizardView);
        document.getElementById('wizard-detect')?.addEventListener('click', () => {
            void wizFetch({ action: 'detect' }).then((a) => { WIZ.data = a; WIZ.message = 'Checked again.'; WIZ.tone = 'ok'; drawWizard(false); })
                .catch((err) => { WIZ.message = `Not checked: ${err.message}`; WIZ.tone = 'bad'; drawWizard(false); });
        });
        showWizardView();
        void wizardFirstRun();
    });
}

if (typeof module !== 'undefined') module.exports = { wizValues, wizField, WIZ };
