// Tutti dashboard: Organizations, teams, roles, priorities and budgets (docs/AUTH.md).
// Uses app.js's el, $, chip and users.js's authPost.
'use strict';

function orgBudgetText(b) {
    if (!b || !Object.keys(b).length) return 'unlimited';
    const parts = [];
    if (b.daily_runs !== undefined) parts.push(`${b.daily_runs} runs/day`);
    if (b.monthly_usd !== undefined) parts.push(`$${b.monthly_usd}/mo`);
    if (b.max_parallel !== undefined) parts.push(`max ${b.max_parallel} parallel`);
    return parts.join(' · ');
}

function orgTeamRow(oid, tid, t) {
    const del = el('button', {
        class: 'btn btn--tonal', type: 'button', text: 'Delete',
        onclick: async () => {
            const ans = await authPost('/api/orgs', { action: 'delete_team', org_id: oid, team_id: tid }, 'Team was not deleted.');
            if (ans) renderOrgs(ans.orgs);
        },
    });
    const proj = (t.projects || []).length ? ` · projects: ${t.projects.join(', ')}` : '';
    return el('li', { class: 'row' },
        el('div', { class: 'row__name' }, `${t.name} (${tid})`, el('span', { class: 'row__kind', text: `${orgBudgetText(t.budget)}${proj}` })),
        chip(`priority ${t.priority}`, 'accent'),
        el('div', { class: 'row__stats' }, del));
}

function orgTeamForm(oid) {
    const form = el('form', { class: 'setting__control' },
        el('input', { class: 'setting__input', name: 'team_id', placeholder: 'team-id', 'aria-label': 'Team id', required: true, size: 10 }),
        el('input', { class: 'setting__input', name: 'name', placeholder: 'Team name', 'aria-label': 'Team name', required: true, size: 14 }),
        el('input', { class: 'setting__input setting__input--short', name: 'priority', type: 'number', min: 1, max: 100, value: 10, title: 'Priority (1-100)', 'aria-label': 'Team priority (1 to 100)' }),
        el('input', { class: 'setting__input setting__input--short', name: 'daily_runs', type: 'number', min: 0, placeholder: 'runs/day', 'aria-label': 'Daily run budget' }),
        el('input', { class: 'setting__input setting__input--short', name: 'monthly_usd', type: 'number', min: 0, step: '0.01', placeholder: 'USD/mo', 'aria-label': 'Monthly USD budget' }),
        el('input', { class: 'setting__input', name: 'projects', placeholder: 'projects (a,b)', 'aria-label': 'Team projects', size: 14 }),
        el('button', { class: 'btn btn--tonal', type: 'submit', text: 'Save team' }));
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const f = new FormData(form);
        const budget = {};
        if (f.get('daily_runs')) budget.daily_runs = Number(f.get('daily_runs'));
        if (f.get('monthly_usd')) budget.monthly_usd = Number(f.get('monthly_usd'));
        const projects = String(f.get('projects') || '').split(',').map((s) => s.trim()).filter(Boolean);
        const ans = await authPost('/api/orgs', {
            action: 'upsert_team', org_id: oid, team_id: f.get('team_id'), name: f.get('name'),
            priority: Number(f.get('priority') || 10), budget, projects,
        }, 'Team was not saved.');
        if (ans) renderOrgs(ans.orgs);
    });
    return form;
}

function orgMemberForm(oid) {
    const roles = ['viewer', 'member', 'admin', 'owner'].map((r) => el('option', { value: r, text: r }));
    const form = el('form', { class: 'setting__control' },
        el('input', { class: 'setting__input', name: 'sub', placeholder: 'user sub or email', 'aria-label': 'Member user id or email', required: true, size: 16 }),
        el('select', { class: 'pick', name: 'role', 'aria-label': 'Member role' }, ...roles),
        el('input', { class: 'setting__input', name: 'teams', placeholder: 'teams (a,b)', 'aria-label': 'Member teams', size: 12 }),
        el('button', { class: 'btn btn--tonal', type: 'submit', text: 'Set member' }));
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const f = new FormData(form);
        const teams = String(f.get('teams') || '').split(',').map((s) => s.trim()).filter(Boolean);
        const ans = await authPost('/api/orgs', {
            action: 'set_member', org_id: oid, sub: f.get('sub'), role: f.get('role'), teams,
        }, 'Member was not updated.');
        if (ans) renderOrgs(ans.orgs);
    });
    return form;
}

function orgSection(oid, org) {
    const teams = Object.entries(org.teams || {});
    const members = Object.entries(org.members || {});
    const teamList = teams.length ? teams.map(([tid, t]) => orgTeamRow(oid, tid, t)) : [el('li', { class: 'row__note', text: 'No teams yet.' })];
    const memberList = members.length ? members.map(([sub, m]) => el('li', { class: 'row' },
        el('div', { class: 'row__name' }, sub, el('span', { class: 'row__kind', text: (m.teams || []).join(', ') || 'no team' })),
        chip(m.role, ROLE_TONE[m.role] || 'ok'))) : [el('li', { class: 'row__note', text: 'No members yet.' })];
    return el('div', { class: 'org-block' },
        el('h3', { class: 'card__sub', text: `${org.name} (${oid})` }),
        el('ul', { class: 'list', 'aria-label': `Teams in ${org.name}` }, ...teamList),
        orgTeamForm(oid),
        el('ul', { class: 'list', 'aria-label': `Members in ${org.name}` }, ...memberList),
        orgMemberForm(oid));
}

function renderOrgs(orgsMap) {
    const host = $('orgs-body');
    if (!host) return;
    const entries = Object.entries(orgsMap || {});
    $('orgs-meta').textContent = `${entries.length} organization${entries.length === 1 ? '' : 's'}`;
    const createForm = el('form', { class: 'setting__control' },
        el('input', { class: 'setting__input', name: 'org_id', placeholder: 'org-id', 'aria-label': 'Organization id', required: true, size: 12 }),
        el('input', { class: 'setting__input', name: 'name', placeholder: 'Organization name', 'aria-label': 'Organization name', required: true, size: 16 }),
        el('button', { class: 'btn btn--filled', type: 'submit', text: 'Create organization' }));
    createForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const f = new FormData(createForm);
        const ans = await authPost('/api/orgs', { action: 'create_org', org_id: f.get('org_id'), name: f.get('name') }, 'Organization was not created.');
        if (ans) renderOrgs(ans.orgs);
    });
    host.replaceChildren(createForm, ...entries.map(([oid, o]) => orgSection(oid, o)));
}

async function loadOrgs() {
    const res = await fetch('/api/orgs', { cache: 'no-store' }).catch(() => null);
    if (res?.ok) renderOrgs((await res.json()).orgs);
}
