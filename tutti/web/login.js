// Tutti sign-in page (also the invite page and the "waiting for approval" page). Google Identity Services gives an ID
// token; the server checks it and sets an HttpOnly session cookie. Text only through textContent.
'use strict';

const meta = (n) => document.querySelector(`meta[name="${n}"]`)?.content || '';
const CLIENT = meta('tutti-client');
const NONCE = meta('tutti-nonce');
const CSRF = meta('tutti-token');
const INVITE = location.pathname === '/invite' ? location.hash.slice(1) : '';
const RETRIED = 'tutti-login-retried';
// the only fragment carried over to the dashboard: a reading from the reading helper (#reading=<url-encoded JSON>)
const READING_RX = /^#reading=[\w%.~!*'()-]*$/;

// Always this dashboard's own root, never a URL from the address: no redirect anywhere else.
function dashboardUrl() {
    const keep = READING_RX.test(location.hash) ? location.hash : '';
    return `/${keep}`;
}
const $ = (id) => document.getElementById(id);

function say(title, text) {
    $('login-title').textContent = title;
    $('login-text').textContent = text;
}

function fail(text) {
    $('login-error').textContent = text;
    $('login-error').hidden = false;
}

function showCanonicalOrigin() {
    if (location.hostname !== '127.0.0.1') return;
    const host = location.port ? `127.0.0.1:${location.port}` : '127.0.0.1';
    $('login-canonical').textContent =
        `This dashboard keeps sign-in on http://${host}; localhost and [::1] redirect here so one session cookie is reused.`;
    $('login-canonical').hidden = false;
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
    location.replace(dashboardUrl());
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

// Google's button works only on localhost and https; a plain LAN address uses the device flow (a code at google.com/device)
const BUTTON_HERE = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname) || location.protocol === 'https:';

function sleep(ms) { return new Promise((resolve) => { setTimeout(resolve, ms); }); }

const REFRESHES = 6;   // a code lasts about 30 minutes: a fresh one (and its QR) up to six times, then the page asks

function showCode(flow) {
    const link = document.createElement('a');
    link.href = flow.verification_url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = flow.verification_url;
    const code = document.createElement('code');
    code.className = 'login__code';
    code.textContent = flow.user_code;
    const steps = document.createElement('p');
    steps.append('On your phone or any device, scan the code or open ', link, ', sign in with Google and enter:');
    const qr = qrSvg(flow.qr_url, 'QR code of Google\'s sign-in page for this code');
    $('login-button').replaceChildren(steps, code, ...(qr ? [qr] : []));
}

// one code: true when signed in (or sent to the waiting page), false when it expired
async function oneCode() {
    const flow = await post('/api/auth/device/start', { invite: INVITE || undefined });
    showCode(flow);
    const until = Date.now() + flow.expires_in * 1000;
    while (Date.now() < until) {
        await sleep(flow.interval * 1000);
        const answer = await post('/api/auth/device/poll', {});
        if (!answer.pending) {
            if (answer.role === 'pending') { location.replace('/'); return true; }
            say('Signed in', `You are ${answer.role === 'admin' ? 'an administrator' : 'a viewer'} of this dashboard.`);
            openDashboard();
            return true;
        }
    }
    return false;
}

async function deviceSignIn() {
    $('login-error').hidden = true;
    for (let i = 0; i < REFRESHES; i++) {
        try {
            if (await oneCode()) return;
        } catch (err) { fail(err.message); return; }
    }
    $('login-button').replaceChildren();
    fail('The codes expired. Reload this page to get a new one.');
}

function signInButton(me) {
    if (BUTTON_HERE && me.client_id) { googleButton(); return; }
    if (!me.device) {
        fail('Sign-in from this address is not set up yet: on the dashboard\'s computer the admin runs `tutti auth setup` and adds the "TVs and Limited Input devices" client. On that computer, http://localhost works now.');
        return;
    }
    const b = document.createElement('button');
    b.className = 'btn btn--filled';
    b.type = 'button';
    b.textContent = 'Sign in with Google';
    b.addEventListener('click', () => { deviceSignIn().catch((err) => fail(err.message)); });
    $('login-button').replaceChildren(b);
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
    showCanonicalOrigin();
    showJoin();
    const me = await fetch('/api/auth/me', { cache: 'no-store' }).then((r) => r.json()).catch(() => null);
    if (!me) { fail('The dashboard is not answering: start it again (`tutti dashboard`), then reload.'); return; }
    if (me.signed_in && me.role !== 'pending' && !INVITE) {
        // signed in, but this page came without the cookie (a link from another site): open the dashboard once
        let retried = false;
        try { retried = sessionStorage.getItem(RETRIED) === '1'; sessionStorage.setItem(RETRIED, '1'); } catch { retried = true; }
        if (!retried) { location.replace(dashboardUrl()); return; }
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
    signInButton(me);
}

start().catch((err) => fail(`The sign-in page failed: ${err.message}. Reload it.`));
