// Tutti dashboard: the one formatter for every percentage and number on every card and tab (pure; a node test runs it).
// Locale-aware (the browser's own locale), sensibly rounded, never float noise: 32.122999999999976 reads "32 %".
'use strict';

const TUTTI_NUMBERS = new Map();

function tuttiNumberFormat(options) {
    const key = JSON.stringify(options);
    if (!TUTTI_NUMBERS.has(key)) TUTTI_NUMBERS.set(key, new Intl.NumberFormat(undefined, options));
    return TUTTI_NUMBERS.get(key);
}

// How many decimals a value earns: none from 10 up, one under 10, two under 1 (and never more than `max`).
function tuttiDigits(v, max = 2) {
    const a = Math.abs(v);
    return Math.min(max, a >= 10 || a === 0 ? 0 : a >= 1 ? 1 : 2);
}

function tuttiFinite(v) {
    const n = typeof v === 'string' && v.trim() !== '' ? Number(v) : v;
    return typeof n === 'number' && Number.isFinite(n) ? n : null;
}

// A number: grouped by the locale, rounded by its size. `digits` fixes the decimals; `none` is shown for a missing value.
function tuttiNum(v, { digits, max = 2, none = '–' } = {}) {
    const n = tuttiFinite(v);
    if (n === null) return none;
    const d = digits ?? tuttiDigits(n, max);
    return tuttiNumberFormat({ minimumFractionDigits: digits ?? 0, maximumFractionDigits: d }).format(n);
}

// A percentage given in points (32.1 means 32.1 %): whole points from 10 up, one decimal under 10, "<1 %" style never needed
// because tiny shares keep one decimal (0.3 %). Locale-aware: "32 %" in ro and de, "32%" in en.
function tuttiPct(v, { digits, none = '–' } = {}) {
    const n = tuttiFinite(v);
    if (n === null) return none;
    const d = digits ?? (Math.abs(n) >= 10 || n === 0 ? 0 : 1);
    return tuttiNumberFormat({ style: 'percent', maximumFractionDigits: d }).format(n / 100);
}

// A share given as a fraction (0.42 means 42 %).
function tuttiShare(v, opts) {
    const n = tuttiFinite(v);
    return n === null ? (opts?.none ?? '–') : tuttiPct(n * 100, opts);
}

// A large count: 1.2 M, 12 k (the locale's compact form).
function tuttiCompact(v, { none = '–' } = {}) {
    const n = tuttiFinite(v);
    if (n === null) return none;
    return tuttiNumberFormat({ notation: 'compact', maximumFractionDigits: 1 }).format(n);
}

// A value for CSS (a meter's width): clamped to 0–100 and rounded, never a long float in a style.
function tuttiCssPct(v) {
    const n = tuttiFinite(v) ?? 0;
    return `${Math.round(Math.min(100, Math.max(0, n)) * 10) / 10}%`;
}

// Float noise in the planner's own sentences (a plan's "heading for 32.122999999999976 %"): a number with four or more
// decimals is rounded like tuttiNum. Applied only to server-computed plan, outlook and action text, never to logs, titles or
// chat, where a 12:30:45.123456 or a 0.12345 is data.
const TUTTI_NOISE = /(?<![\d.])\d+\.\d{4,}(?![\d.])/g;

function tuttiTidy(text) {
    return typeof text === 'string' ? text.replace(TUTTI_NOISE, (m) => tuttiNum(Number(m))) : text;
}

if (typeof module !== 'undefined') module.exports = { tuttiNum, tuttiPct, tuttiShare, tuttiCompact, tuttiCssPct, tuttiTidy };
