// setup.js: the Set up card. What `tutti setup` found, the defaults it picked (Change) and what only you can do
// (<state>/setup.json, served in /api/state as
// `setup`); every row left has its exact command (with a Copy button) or a one-click fix, a POST to /api/setup that runs
// `tutti setup --yes` (it only fills what is missing). Uses app.js's helpers (el, chip, $, empty, relative, refresh,
// showBanner, TOKEN).

const SETUP_TONE = { ok: 'ok', todo: 'warn', info: 'idle', default: 'accent' };

async function runSetup(button) {
    button.disabled = true;
    try {
        const res = await fetch('/api/setup', {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN }, body: JSON.stringify({ action: 'run' }),
        });
        const answer = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
        showBanner(el('strong', { text: 'Setup is running. ' }), 'This card updates when it is done (about a minute).');
    } catch (err) {
        showBanner(el('strong', { text: 'Setup did not start. ' }), err.message);
    } finally {
        button.disabled = false;
    }
}

function copyButton(command) {
    return el('button', {
        class: 'btn btn--small', type: 'button', text: 'Copy', 'aria-label': `Copy: ${command}`,
        onclick: (e) => navigator.clipboard.writeText(command).then(() => { e.target.textContent = 'Copied'; }, () => {}),
    });
}

function setupRow(r) {
    let action = null;
    // a default setup picked: Change opens its key in the Settings tab (or names it in the config file)
    if (r.state === 'default') action = el('a', { class: 'btn btn--small', href: `#settings/${r.change}`, text: 'Change',
        title: `Change ${r.change} (Settings, or the config file)` });
    else if (r.state !== 'ok') action = r.fix
        ? el('button', { class: 'btn btn--small', type: 'button', text: 'Fix', onclick: (e) => runSetup(e.target) })
        : (r.command && copyButton(r.command));
    return el('li', { class: 'row' },
        el('div', { class: 'row__name', text: r.title }),
        chip({ todo: 'only you', default: 'default' }[r.state] || r.state, SETUP_TONE[r.state]),
        el('p', { class: 'row__note', text: r.detail }),
        r.command && r.state !== 'ok' ? el('code', { class: 'row__note', text: r.command }) : null,
        action || null);
}

function renderSetup(s) {
    const r = s.setup;
    if (!r) {
        $('setup-meta').textContent = 'Not run on this computer yet';
        $('setup').replaceChildren(el('li', {}, empty('Run tutti setup', 'It finds your agents, machines and projects and fills the config.'),
            el('button', { class: 'btn btn--small', type: 'button', text: 'Run setup', onclick: (e) => runSetup(e.target) })));
        return;
    }
    const left = r.left ? `${r.left} left` : 'All set';
    $('setup-meta').textContent = `${left} · checked ${relative(r.generated_at)}`;
    const rank = { todo: 0, default: 1 };
    const rows = [...r.items].sort((a, b) => (rank[a.state] ?? 2) - (rank[b.state] ?? 2));
    $('setup').replaceChildren(...rows.map(setupRow),
        el('li', {}, el('button', { class: 'btn btn--small', type: 'button', text: 'Check again', onclick: (e) => runSetup(e.target) })));
}
