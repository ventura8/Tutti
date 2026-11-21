// Tutti dashboard: move and resize the big cards (t447). Order + column span + optional height, clamped, saved per user.
// Mirrors tutti/layout_cards.py. On a narrow screen cards stay full width (no drag); resize height still works.
'use strict';

const LAYOUT_COLS_MIN = 4, LAYOUT_COLS_MAX = 12, LAYOUT_HEIGHT_MIN = 140, LAYOUT_HEIGHT_MAX = 1200;
const LAYOUT_DEFAULT_ORDER = ['usage', 'machines', 'attention', 'household', 'jobs-stack', 'side-stack',
    'autopilot', 'work-browser', 'projects', 'security', 'scouts', 'connect', 'outside', 'where', 'setup',
    'controls', 'selfimprove', 'aiassist', 'insights', 'my-schedule', 'my-questions', 'my-github', 'jobs', 'vpn', 'users'];

const LAYOUT = { user: 'local', order: [...LAYOUT_DEFAULT_ORDER], sizes: {}, drag: null, saveTimer: 0 };

function layoutClampCols(n) {
    n = parseInt(n, 10);
    if (!Number.isFinite(n)) return LAYOUT_COLS_MAX;
    return Math.max(LAYOUT_COLS_MIN, Math.min(LAYOUT_COLS_MAX, n));
}

function layoutClampHeight(n) {
    n = parseInt(n, 10);
    if (!Number.isFinite(n) || n <= 0) return 0;
    return Math.max(LAYOUT_HEIGHT_MIN, Math.min(LAYOUT_HEIGHT_MAX, n));
}

function layoutNormalize(saved) {
    const known = LAYOUT_DEFAULT_ORDER;
    const seen = new Set();
    const order = [];
    for (const id of (saved && saved.order) || []) {
        if (known.includes(id) && !seen.has(id)) { order.push(id); seen.add(id); }
    }
    for (const id of known) if (!seen.has(id)) order.push(id);
    const sizes = {};
    for (const id of known) {
        const s = (saved && saved.sizes && saved.sizes[id]) || {};
        sizes[id] = { cols: layoutClampCols(s.cols ?? 12), height: layoutClampHeight(s.height ?? 0) };
        if (id === 'jobs-stack') sizes[id].cols = layoutClampCols(s.cols ?? 7);
        if (id === 'side-stack') sizes[id].cols = layoutClampCols(s.cols ?? 5);
    }
    return { order, sizes };
}

function layoutStorageKey() {
    return `tutti-dash-layout:${LAYOUT.user}`;
}

function layoutWide() {
    return window.matchMedia('(min-width: 960px)').matches;
}

function layoutItems() {
    const grid = document.getElementById('dashboard-grid') || document.querySelector('#dashboard-view .grid');
    if (!grid) return [];
    return [...grid.querySelectorAll(':scope > [data-layout-id]')];
}

function layoutApply() {
    const grid = document.getElementById('dashboard-grid') || document.querySelector('#dashboard-view .grid');
    if (!grid) return;
    const byId = Object.fromEntries(layoutItems().map((el) => [el.dataset.layoutId, el]));
    for (const id of LAYOUT.order) {
        const el = byId[id];
        if (el) grid.appendChild(el);
    }
    for (const el of layoutItems()) {
        const id = el.dataset.layoutId;
        const size = LAYOUT.sizes[id] || { cols: 12, height: 0 };
        el.style.gridColumn = layoutWide() ? `span ${size.cols}` : '';
        if (size.height > 0) {
            el.style.minHeight = `${size.height}px`;
            el.style.maxHeight = `${LAYOUT_HEIGHT_MAX}px`;
            el.style.overflow = 'auto';
        } else {
            el.style.minHeight = '';
            el.style.maxHeight = '';
            el.style.overflow = '';
        }
        el.classList.toggle('card--layout', true);
    }
}

function layoutEnsureChrome(el) {
    if (el.querySelector(':scope > .card__layout')) return;
    const bar = document.createElement('div');
    bar.className = 'card__layout';
    const handle = document.createElement('button');
    handle.type = 'button';
    handle.className = 'card__drag btn btn--text';
    handle.title = 'Drag to move this card';
    handle.setAttribute('aria-label', 'Move card');
    handle.textContent = 'Move';
    const grip = document.createElement('span');
    grip.className = 'card__resize';
    grip.title = 'Drag to resize';
    grip.setAttribute('aria-hidden', 'true');
    bar.append(handle, grip);
    el.insertBefore(bar, el.firstChild);
    handle.addEventListener('pointerdown', (e) => layoutDragStart(e, el));
    grip.addEventListener('pointerdown', (e) => layoutResizeStart(e, el));
}

function layoutDragStart(e, el) {
    if (!layoutWide() || e.button !== 0) return;
    e.preventDefault();
    LAYOUT.drag = { kind: 'move', el, startY: e.clientY };
    el.classList.add('card--dragging');
    el.setPointerCapture?.(e.pointerId);
    const move = (ev) => {
        if (!LAYOUT.drag || LAYOUT.drag.kind !== 'move') return;
        const items = layoutItems().filter((n) => n !== el);
        const y = ev.clientY;
        let target = null;
        for (const other of items) {
            const r = other.getBoundingClientRect();
            if (y < r.top + r.height / 2) { target = other; break; }
        }
        const grid = el.parentElement;
        if (target) grid.insertBefore(el, target);
        else grid.appendChild(el);
    };
    const up = () => {
        el.classList.remove('card--dragging');
        LAYOUT.drag = null;
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
        layoutReadDom();
        layoutScheduleSave();
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
}

function layoutResizeStart(e, el) {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const id = el.dataset.layoutId;
    const start = { x: e.clientX, y: e.clientY, cols: LAYOUT.sizes[id]?.cols || 12, height: el.getBoundingClientRect().height };
    LAYOUT.drag = { kind: 'resize', el, start };
    el.classList.add('card--resizing');
    const grid = el.parentElement;
    const move = (ev) => {
        if (!LAYOUT.drag || LAYOUT.drag.kind !== 'resize') return;
        const dx = ev.clientX - start.x;
        const dy = ev.clientY - start.y;
        if (layoutWide()) {
            const colW = grid.getBoundingClientRect().width / 12;
            LAYOUT.sizes[id].cols = layoutClampCols(start.cols + Math.round(dx / colW));
            el.style.gridColumn = `span ${LAYOUT.sizes[id].cols}`;
        }
        const h = layoutClampHeight(Math.round(start.height + dy));
        LAYOUT.sizes[id].height = h;
        el.style.minHeight = h ? `${h}px` : '';
        el.style.overflow = h ? 'auto' : '';
    };
    const up = () => {
        el.classList.remove('card--resizing');
        LAYOUT.drag = null;
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
        layoutScheduleSave();
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
}

function layoutReadDom() {
    LAYOUT.order = layoutItems().map((el) => el.dataset.layoutId);
}

function layoutScheduleSave() {
    clearTimeout(LAYOUT.saveTimer);
    LAYOUT.saveTimer = setTimeout(() => { void layoutSave(); }, 300);
}

async function layoutSave() {
    const body = { order: LAYOUT.order, sizes: LAYOUT.sizes };
    try { localStorage.setItem(layoutStorageKey(), JSON.stringify(body)); } catch { /* private window */ }
    try {
        const r = await fetch('/api/dashboard-layout', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body), credentials: 'same-origin',
        });
        if (r.ok) {
            const data = await r.json();
            if (data && data.layout) Object.assign(LAYOUT, layoutNormalize(data.layout));
        }
    } catch { /* offline: local copy is enough until the next online save */ }
}

async function layoutLoad(userKey) {
    LAYOUT.user = userKey || 'local';
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(layoutStorageKey()) || 'null'); } catch { saved = null; }
    try {
        const r = await fetch('/api/dashboard-layout', { credentials: 'same-origin' });
        if (r.ok) {
            const data = await r.json();
            if (data && data.layout) saved = data.layout;
        }
    } catch { /* use local */ }
    Object.assign(LAYOUT, layoutNormalize(saved));
    layoutApply();
}

function layoutReset() {
    Object.assign(LAYOUT, layoutNormalize(null));
    layoutApply();
    layoutScheduleSave();
}

function initLayout() {
    const grid = document.getElementById('dashboard-grid') || document.querySelector('#dashboard-view .grid');
    if (!grid) return;
    for (const el of layoutItems()) layoutEnsureChrome(el);
    document.getElementById('layout-reset')?.addEventListener('click', layoutReset);
    window.addEventListener('resize', () => layoutApply());
    const boot = () => {
        const who = document.body.dataset.user || document.getElementById('me')?.textContent?.trim() || 'local';
        void layoutLoad(who);
    };
    if (document.body.dataset.role) boot();
    else window.addEventListener('tutti-ready', boot, { once: true });
    // fallback: load after a tick once auth paints
    setTimeout(boot, 800);
}

if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('DOMContentLoaded', initLayout);
}
if (typeof module !== 'undefined') {
    module.exports = { layoutNormalize, layoutClampCols, layoutClampHeight, LAYOUT_DEFAULT_ORDER };
}
