// Tutti dashboard: who is signed in, the viewer's read-only mode, and the admin's Users card (approve, change role,
// remove, invite with a link and a QR code). Uses app.js's helpers (el, $, chip, relative, showBanner, TOKEN) and qr.js.
// Hiding is for clarity only: the server refuses every change a viewer sends (tutti/authweb.py).
'use strict';

const ROLE_TONE = { admin: 'accent', viewer: 'ok', pending: 'warn' };

async function authPost(path, body, failText) {
    try {
        const res = await fetch(path, {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN }, body: JSON.stringify(body),
        });
        const answer = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
        return answer;
    } catch (err) {
        showBanner(el('strong', { text: `${failText} ` }), err.message);
        return null;
    }
}

const seen = (t) => (t ? relative(new Date(t * 1000).toISOString()) : 'never');

function userActions(u, me) {
    const act = (text, body, ask) => el('button', {
        class: 'btn btn--tonal', type: 'button', text,
        onclick: async () => {
            if (ask && !window.confirm(ask)) return;
            const answer = await authPost('/api/auth/users', { ...body, sub: u.sub }, `${u.email} was not changed.`);
            if (answer) renderUsers(answer, me);
        },
    });
    const out = [];
    if (u.role === 'pending') out.push(act('Approve', { action: 'approve' }));
    if (u.role !== 'admin') out.push(act('Make admin', { action: 'role', role: 'admin' }, `Make ${u.email} an administrator? They can then change everything, Settings included.`));
    if (u.role === 'admin') out.push(act('Make viewer', { action: 'role', role: 'viewer' }, `Make ${u.email} a viewer? They can then only look.`));
    out.push(act('Remove', { action: 'remove' }, `Remove ${u.email}? They are signed out and must be approved again to come back.`));
    return out;
}

function userRow(u, me) {
    const tutti = u.tutti ? `Tutti joined ${seen(u.tutti.joined)}, last seen ${seen(u.tutti.last_seen)}` : 'no Tutti joined';
    return el('li', { class: 'row' },
        el('div', { class: 'row__name' }, u.email || u.sub, el('span', { class: 'row__kind', text: `${u.name || ''}${u.email === me.email ? ' (you)' : ''}` })),
        chip(u.role, ROLE_TONE[u.role]),
        el('div', { class: 'row__stats' }, el('span', { text: `last seen ${seen(u.last_seen)}` }), el('span', { text: tutti }), ...userActions(u, me)));
}

function inviteRow(i, me) {
    const state = i.revoked ? 'revoked' : i.live ? `open until ${new Date(i.expires * 1000).toLocaleString()}` : i.used.length ? 'used' : 'expired';
    const revoke = i.live ? el('button', {
        class: 'btn btn--tonal', type: 'button', text: 'Revoke',
        onclick: async () => {
            const answer = await authPost('/api/auth/invites', { action: 'revoke', id: i.id }, 'The invite was not revoked.');
            if (answer) renderUsers(answer, me);
        },
    }) : null;
    return el('li', { class: 'row' },
        el('div', { class: 'row__name' }, `Invite ${i.id.slice(0, 6)}`, el('span', { class: 'row__kind', text: `as ${i.role}` })),
        chip(state, i.live ? 'ok' : 'idle'), el('div', { class: 'row__stats' }, revoke));
}

function showInvite(answer) {
    const copy = (text, label) => el('button', {
        class: 'btn btn--tonal', type: 'button', text: label,
        onclick: async (e) => {
            try { await navigator.clipboard.writeText(text); e.target.textContent = 'Copied'; } catch { showBanner(el('strong', { text: 'Copying was refused. ' }), 'Select the text and copy it by hand.'); }
        },
    });
    const qr = qrSvg(answer.url, 'QR code of the invite link');
    $('invite-new').replaceChildren(
        el('p', { class: 'row__note', text: `Send this link to the person you invite (as ${answer.role}). It works once and for 7 days; it is shown only now.` }),
        el('div', { class: 'invite__link' }, el('code', { text: answer.url }), copy(answer.url, 'Copy link')),
        qr || el('p', { class: 'row__note', text: 'The link is too long for a QR code: send the link.' }),
        el('p', { class: 'row__note', text: 'On their own computer, their Tutti joins the household with:' }),
        el('div', { class: 'invite__link' }, el('code', { text: answer.join }), copy(answer.join, 'Copy command')));
    $('invite-new').hidden = false;
}

function renderUsers(data, me) {
    $('users').replaceChildren(...data.users.map((u) => userRow(u, me)));
    $('invites').replaceChildren(...(data.invites.length ? data.invites.map((i) => inviteRow(i, me)) : [el('li', { class: 'row__note', text: 'No invites yet.' })]));
    const waiting = data.users.filter((u) => u.role === 'pending').length;
    $('users-meta').textContent = `${data.users.length} signed in${waiting ? `, ${waiting} waiting for approval` : ''}`;
}

async function loadUsers(me) {
    const res = await fetch('/api/auth/users', { cache: 'no-store' }).catch(() => null);
    if (res?.ok) renderUsers(await res.json(), me);
}

async function startAuth() {
    const me = await fetch('/api/auth/me', { cache: 'no-store' }).then((r) => r.json()).catch(() => null);
    if (!me) return;
    if (!me.enabled) {
        $('auth-banner').replaceChildren(el('strong', { text: 'Sign-in is off: ' }),
            'this dashboard opens on this computer only. To sign in with Google and let others in, run ', el('code', { text: 'tutti auth setup' }),
            ' (docs/AUTH.md).');
        $('auth-banner').hidden = false;
        return;
    }
    document.body.dataset.role = me.role || 'viewer';
    $('me').textContent = `${me.email} · ${me.role}`;
    $('me').hidden = false;
    $('signout').hidden = false;
    $('signout').addEventListener('click', async () => {
        if (await authPost('/api/auth/logout', {}, 'You were not signed out.')) location.replace('/');
    });
    if (me.role !== 'admin') return;
    $('users-card').hidden = false;
    $('invite-create').addEventListener('click', async () => {
        const answer = await authPost('/api/auth/invites', { action: 'create', role: $('invite-role').value }, 'No invite was created.');
        if (answer) { showInvite(answer); renderUsers(answer, me); }
    });
    await loadUsers(me);
    setInterval(() => loadUsers(me), 60000);
}

startAuth();
