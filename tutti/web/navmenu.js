// Tutti dashboard: the views hamburger (t446). Replaces the old tab bar; same #chat / #dashboard / #settings / #users
// hashes. Open/close, Escape, outside click, focus return; the toggle shows the current view's name.
'use strict';

const NAV_LABELS = { chat: 'Chat', dashboard: 'Dashboard', tasks: 'Tasks', schedule: 'Schedule', devices: 'Devices', machines: 'Machines', discovered: 'Discovered', agents: 'Agents', settings: 'Settings', users: 'Users' };

// Every view, by its hash (#tasks, #settings/…); one list so a new tab is one entry here, its menu link and its <x-view>.
const TUTTI_VIEWS = Object.keys(NAV_LABELS);

// The view a hash shows: its first word when it names a view (Users only for an admin), else the Chat (the default).
function tuttiView(hash) {
    const name = String(hash || '').replace(/^#/, '').split('/')[0];
    const admin = typeof document !== 'undefined' && document.body?.dataset.role === 'admin';
    return TUTTI_VIEWS.includes(name) && (name !== 'users' || admin) ? name : 'chat';
}

function navMenuEls() {
    return {
        btn: document.getElementById('nav-menu-btn'),
        panel: document.getElementById('nav-menu-panel'),
        current: document.getElementById('nav-menu-current'),
        wrap: document.querySelector('.nav-menu'),
    };
}

function navMenuView() {
    return tuttiView(location.hash);
}

function navMenuSync() {
    const { btn, panel, current } = navMenuEls();
    if (!btn || !panel) return;
    const view = navMenuView();
    if (current) current.textContent = NAV_LABELS[view] || 'Chat';
    for (const a of panel.querySelectorAll('[data-view]')) {
        if (a.dataset.view === view) a.setAttribute('aria-current', 'page');
        else a.removeAttribute('aria-current');
    }
    btn.setAttribute('aria-label', `Views: ${NAV_LABELS[view] || 'Chat'}`);
}

function navMenuOpen() {
    const { btn, panel } = navMenuEls();
    if (!btn || !panel || !panel.hidden) return;
    panel.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    const cur = panel.querySelector('[aria-current="page"]') || panel.querySelector('[data-view]');
    (cur || panel).focus?.();
    cur?.focus?.();
}

function navMenuClose(returnFocus) {
    const { btn, panel } = navMenuEls();
    if (!btn || !panel || panel.hidden) return;
    panel.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
    if (returnFocus !== false) btn.focus();
}

function navMenuToggle() {
    const { panel } = navMenuEls();
    if (!panel) return;
    if (panel.hidden) navMenuOpen();
    else navMenuClose();
}

function initNavMenu() {
    const { btn, panel, wrap } = navMenuEls();
    if (!btn || !panel) return;
    btn.addEventListener('click', (e) => { e.stopPropagation(); navMenuToggle(); });
    panel.addEventListener('click', (e) => {
        if (e.target.closest?.('[data-view]')) navMenuClose(false);
    });
    document.addEventListener('click', (e) => {
        if (!panel.hidden && wrap && !wrap.contains(e.target)) navMenuClose(false);
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !panel.hidden) {
            e.preventDefault();
            navMenuClose();
        }
    });
    window.addEventListener('hashchange', () => { navMenuClose(false); navMenuSync(); });
    navMenuSync();
}

if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('DOMContentLoaded', initNavMenu);
}
if (typeof module !== 'undefined') {
    module.exports = { navMenuView, navMenuSync, navMenuOpen, navMenuClose, navMenuToggle, NAV_LABELS, TUTTI_VIEWS, tuttiView };
}
