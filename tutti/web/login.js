// Tutti sign-in page (also the invite page and the "waiting for approval" page). Google Identity Services gives an ID
// token; the server checks it and sets an HttpOnly session cookie. Text only through textContent.
'use strict';

const meta = (n) => document.querySelector(`meta[name="${n}"]`)?.content || '';
const CLIENT = meta('tutti-client');
const NONCE = meta('tutti-nonce');
const CSRF = meta('tutti-token');
const INVITE = location.pathname === '/invite' ? location.hash.slice(1) : '';
const RETRIED = 'tutti-login-retried';
const $ = (id) => document.getElementById(id);

function say(title, text) {
    $('login-title').textContent = title;
    $('login-text').textContent = text;
}

function fail(text) {
    $('login-error').textContent = text;
    $('login-error').hidden = false;
}

async function post(path, body, token) {
    const res = await fetch(path, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': token || '' }, body: JSON.stringify(body),
    });
    const answer = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
    return answer;
}

function openDashboard() {
    if (INVITE) {
        $('login-button').replaceChildren();
        $('login-open').hidden = false;
        return;
    }
    location.replace(`/${location.hash}`);
}

async function onCredential(response) {
    $('login-error').hidden = true;
    try {
        const answer = await post('/api/auth/login', { credential: response.credential, invite: INVITE || undefined });
        if (answer.role === 'pending') { location.replace('/'); return; }
        say('Signed in', `You are ${answer.role === 'admin' ? 'an administrator' : 'a viewer'} of this dashboard.`);
        openDashboard();
    } catch (err) {
        fail(err.message);
    }
}

function googleButton() {
    const s = document.createElement('script');
    s.src = 'https://accounts.google.com/gsi/client';
    s.async = true;
    s.onload = () => {
        google.accounts.id.initialize({ client_id: CLIENT, nonce: NONCE, callback: onCredential, auto_select: false, ux_mode: 'popup' });
        google.accounts.id.renderButton($('login-button'), { theme: 'filled_blue', size: 'large', text: 'signin_with', shape: 'pill' });
    };
    s.onerror = () => fail('Google\'s sign-in could not be loaded: check the internet connection, then reload this page.');
    document.head.append(s);
}

function showJoin() {
    if (!INVITE) return;
    $('join-cmd').textContent = `tutti household join ${location.href}`;
    $('join-install').href = meta('tutti-install');
    $('join-copy').addEventListener('click', async () => {
        try {
            await navigator.clipboard.writeText($('join-cmd').textContent);
            $('join-copy').textContent = 'Copied';
        } catch {
            fail('Copying was refused by the browser: select the command and copy it by hand.');
        }
    });
    $('login-join').hidden = false;
}

async function start() {
    showJoin();
    const me = await fetch('/api/auth/me', { cache: 'no-store' }).then((r) => r.json()).catch(() => null);
    if (!me) { fail('The dashboard is not answering: start it again (`tutti dashboard`), then reload.'); return; }
    if (me.signed_in && me.role !== 'pending' && !INVITE) {
        // signed in, but this page came without the cookie (a link from another site): open the dashboard once
        let retried = false;
        try { retried = sessionStorage.getItem(RETRIED) === '1'; sessionStorage.setItem(RETRIED, '1'); } catch { retried = true; }
        if (!retried) { location.replace(location.href); return; }
        say('Signed in', 'Open the dashboard to continue.');
        $('login-open').hidden = false;
        return;
    }
    if (me.signed_in && me.role === 'pending' && !INVITE) {
        say('Waiting for approval', `You are signed in as ${me.email}. An administrator of this dashboard has to approve you; this page opens the dashboard once they have.`);
        $('login-out').hidden = !CSRF;
        $('login-out').addEventListener('click', async () => {
            try { await post('/api/auth/logout', {}, CSRF); location.replace('/'); } catch (err) { fail(err.message); }
        });
        setTimeout(() => location.replace('/'), 30000);
        return;
    }
    if (me.first) say('Welcome to Tutti', 'Sign in with Google to become the administrator of this dashboard.');
    else if (INVITE) say('You are invited', 'Sign in with Google to open this Tutti dashboard; the invite approves you at once.');
    else say('Sign in', 'Sign in with Google to open this Tutti dashboard. A new account waits for an administrator\'s approval.');
    googleButton();
}

start();
