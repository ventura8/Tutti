// The Chat's controls (docs/CHAT.md, *Controls*): model, effort and mode pickers, the context meter and token count, plan
// cards, slash commands, @-mentions, pasting screenshots and logs, and keyboard shortcuts. Loaded after chat.js (CHAT, el,
// chatApi, say, send...). The pure parts at the top run in node for the tests.
'use strict';

const SLASH = [
    { cmd: '/new', help: 'start a new chat' },
    { cmd: '/model', help: 'who answers: auto, local, online or a model', arg: true },
    { cmd: '/effort', help: 'auto, low, medium, high or max', arg: true },
    { cmd: '/mode', help: 'auto (autonomous), ask (before acting), plan (review a plan) or answer (just ask)', arg: true },
    { cmd: '/clear', help: 'empty this chat (its settings stay)' },
    { cmd: '/help', help: 'commands and keyboard shortcuts' },
    // work commands, sent to Tutti as a message (tutti/chat.py COMMANDS; owner 2026-11-17)
    { cmd: '/delegate', help: 'hand work to the best agent now: /delegate [project] what', arg: true, send: true },
    { cmd: '/task', help: 'queue a task: /task project what', arg: true, send: true },
    { cmd: '/tasks', help: 'the task queue (/tasks all: with done ones)', send: true },
    { cmd: '/status', help: 'what is running, or one run: /status [run id]', send: true },
    { cmd: '/usage', help: 'every account against its pace', send: true },
    { cmd: '/hosts', help: 'the computers and their state', send: true },
    { cmd: '/devices', help: 'phones, tablets and TVs', send: true },
    { cmd: '/pause', help: 'pause a host, device, agent or account', arg: true, send: true },
    { cmd: '/resume', help: 'resume a host, device, agent or account', arg: true, send: true },
    { cmd: '/ask', help: 'ask a scout: /ask grok question', arg: true, send: true },
    { cmd: '/tests', help: 'queue the full test suite: /tests project', arg: true, send: true },
];
const MODE_WORDS = { autonomous: 'auto', auto: 'auto', ask: 'ask', confirm: 'ask', plan: 'plan', review: 'plan', answer: 'answer', just: 'answer' };
const SHORTCUTS = [['Enter', 'send'], ['Shift+Enter', 'a new line'], ['Esc', 'stop the answer, or close a list'],
    ['↑ (empty box)', 'edit your last message'], ['/', 'commands; from anywhere in the Chat: go to the message box'],
    ['@', 'add a project, job, machine or file (@project/path) as context'], ['Ctrl+K or ⌘K', 'search your chats'],
    ['Alt+N', 'a new chat'], ['?', 'this list']];

// "/mode plan" -> {cmd: '/mode', arg: 'plan'}; anything that is not a known command -> null.
function parseSlash(text) {
    const m = /^(\/[a-z]+)(?:\s+(.*))?$/i.exec(String(text || '').trim());
    if (!m) return null;
    const cmd = m[1].toLowerCase();
    return SLASH.some((c) => c.cmd === cmd) ? { cmd, arg: (m[2] || '').trim() } : { cmd, arg: '', unknown: true };
}

// The word being typed at the caret: {start, word} (a word starts after a space or at the start).
function tokenAt(text, caret) {
    const before = String(text || '').slice(0, caret);
    const start = Math.max(before.lastIndexOf(' '), before.lastIndexOf('\n')) + 1;
    return { start, word: before.slice(start) };
}

// What the popup offers for the text at the caret: slash commands at the start, @-mentions anywhere, or nothing.
function popupFor(text, caret) {
    const t = tokenAt(text, caret);
    if (t.start === 0 && t.word.startsWith('/') && !String(text).slice(0, caret).includes(' ')) {
        return { kind: 'slash', query: t.word.toLowerCase(), start: 0 };
    }
    if (t.word.startsWith('@')) return { kind: 'mention', query: t.word.slice(1).replace(/^(project|job|machine|file):/, ''), start: t.start };
    return null;
}

const shortNum = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n || 0));

// The usage line under the composer: tokens in and out, and what they would cost at API prices (local models: free).
function usageText(u) {
    if (!u || !(u.in || u.out)) return 'No tokens used yet';
    const cost = u.usd > 0 ? ` · ≈ $${u.usd < 0.01 ? '<0.01' : u.usd.toFixed(2)} at API prices` : ' · local: no cost';
    return `${shortNum(u.in)} in · ${shortNum(u.out)} out${cost}`;
}

// ---- the pickers

const ctl = { popup: null, items: [], active: -1, timer: 0 };

function currentSettings() {
    const s = { ...(CHAT.defaults || {}), ...(CHAT.settings || {}) };
    return { model: s.model || 'auto', effort: s.effort || 'auto', mode: s.mode || 'auto' };
}

function fillSelect(box, options, value) {
    box.replaceChildren(...options);
    box.value = value;
}

function drawControls() {
    const s = currentSettings();
    const groups = { mode: el('optgroup', { label: 'Mode' }), local: el('optgroup', { label: 'Local models' }), online: el('optgroup', { label: 'Online agents' }) };
    for (const m of CHAT.models || []) {
        groups[m.group].append(el('option', { value: m.id, text: m.available ? m.label : `${m.label} (not now: ${m.why})` }));
    }
    fillSelect(cq('chat-model'), Object.values(groups).filter((g) => g.children.length), s.model);
    const efforts = (CHAT.efforts?.length ? CHAT.efforts : ['auto', 'low', 'medium', 'high', 'max']);
    fillSelect(cq('chat-effort'), efforts.map((e) => el('option', { value: e, text: e === 'auto' ? 'Auto' : e[0].toUpperCase() + e.slice(1) })), s.effort);
    const modes = Object.keys(CHAT.modes || {}).length ? CHAT.modes : { auto: 'Autonomous', ask: 'Ask before acting', plan: 'Plan review' };
    fillSelect(cq('chat-mode'), Object.entries(modes).map(([k, v]) => el('option', { value: k, text: v })), s.mode);
    drawUsage();
}

function drawUsage() {
    const u = CHAT.conv?.usage || null;
    const pct = Math.min(100, Math.max(0, u?.context_pct || 0));
    cq('chat-context').value = pct;
    cq('chat-context-pct').textContent = `${pct} %`;
    const at = CHAT.defaults?.compact_pct || CHAT.compactDefault || 60;
    cq('chat-context').title = `Context ${pct} % full; older messages are summarised at ${at} %`;
    cq('chat-usage').textContent = usageText(u);
}

async function setControl(key, value, label) {
    try {
        const data = await chatApi({ action: 'settings', id: CHAT.conv?.id || undefined, [key]: value });
        CHAT.defaults = { ...(CHAT.defaults || {}), [key]: value };
        if (CHAT.conv?.id) CHAT.settings = data.settings;
        say(`${label}: ${value}.`);
    } catch (err) {
        say(err.message);
    }
    drawControls();
}

// ---- plan cards: review, edit, run as a whole

function planStep(m, c, st) {
    if (c.state !== 'plan') {
        const tone = { done: 'ok', failed: 'bad', refused: 'warn', skipped: 'idle', undone: 'idle', pending: 'idle' }[st.state] || 'idle';
        const row = el('li', { class: 'plan__step', 'data-state': st.state },
            el('span', { class: 'chip', 'data-tone': tone, text: st.state }), ' ', st.label,
            st.text ? el('span', { class: 'act__text', text: st.text }) : null);
        if (st.state === 'done' && st.undo) row.append(el('button', { class: 'btn btn--text', type: 'button', text: 'Undo', onclick: (e) => undoStep(m, c, st, e.currentTarget) }));
        return row;
    }
    const key = ['title', 'task', 'question'].find((k) => k in (st.call?.args || {}));
    const keep = el('input', { type: 'checkbox', checked: true, id: `${c.id}-${st.id}-keep`, 'data-step': st.id });
    const text = key ? el('input', { class: 'plan__text setting__input', id: `${c.id}-${st.id}-text`, value: st.call.args[key], 'aria-label': `Step ${st.id.slice(1)}: what it says`, maxlength: 300 }) : null;
    return el('li', { class: 'plan__step' }, keep, el('label', { for: keep.id, text: st.label.split(':')[0] }), text);
}

function planCard(m, c) {
    const tone = { plan: 'accent', running: 'accent', done: 'ok', cancelled: 'idle' }[c.state] || 'idle';
    const title = { plan: 'Review the plan', running: 'Running the plan…', done: 'The plan ran', cancelled: 'Plan cancelled' }[c.state] || c.label;
    const card = el('section', { class: 'act plan', 'data-tone': tone, 'data-card': c.id, 'aria-label': title },
        el('p', { class: 'act__title' }, el('span', { class: 'chip', 'data-tone': tone, text: title }), ' ', c.label),
        c.text ? el('p', { class: 'act__text', text: c.text }) : null,
        el('ol', { class: 'plan__steps' }, ...(c.steps || []).map((st) => planStep(m, c, st))));
    if (c.state === 'plan') {
        card.append(el('div', { class: 'act__btns' },
            el('button', { class: 'btn btn--filled', type: 'button', text: 'Run the plan', onclick: (e) => runPlan(m, c, true, card, e.currentTarget) }),
            el('button', { class: 'btn btn--text', type: 'button', text: 'Cancel', onclick: (e) => runPlan(m, c, false, card, e.currentTarget) })));
    }
    return card;
}

async function runPlan(m, c, yes, card, button) {
    const steps = yes ? [...card.querySelectorAll('input[type=checkbox][data-step]')].filter((b) => b.checked).map((b) => {
        const text = card.querySelector(`#${CSS.escape(`${c.id}-${b.dataset.step}-text`)}`);
        return { id: b.dataset.step, text: text ? text.value : undefined };
    }) : undefined;
    if (yes && !steps.length) { say('Keep at least one step, or Cancel the plan.'); return; }
    button.disabled = true;
    try {
        const data = await chatApi({ action: 'plan', conversation: CHAT.conv.id, message: m.id, card: c.id, steps, yes });
        Object.assign(c, data.card);
        card.replaceWith(planCard(m, c));
        say(yes ? `The plan ran: ${data.card.text}` : 'Plan cancelled: nothing was done.');
    } catch (err) {
        button.disabled = false;
        say(err.message);
    }
}

async function undoStep(m, c, st, button) {
    button.disabled = true;
    try {
        const data = await chatApi({ action: 'undo', conversation: CHAT.conv.id, message: m.id, card: c.id, step: st.id });
        CHAT.conv = data.conversation;
        drawLog();
        say(`Undone: ${st.label}`);
    } catch (err) {
        button.disabled = false;
        say(err.message);
    }
}

// ---- slash commands

function matchModel(arg) {
    const a = arg.toLowerCase();
    return (CHAT.models || []).find((m) => m.id.toLowerCase() === a || m.label.toLowerCase().startsWith(a))?.id;
}

async function runSlash(text) {
    const s = parseSlash(text);
    if (!s) return false;
    if (s.unknown) { say(`${s.cmd} is not a command. Type / to see them.`); return true; }
    if (SLASH.find((c) => c.cmd === s.cmd)?.send) return false;   // a work command: sent as a message, Tutti runs it
    const arg = s.arg.toLowerCase();
    if (s.cmd === '/new') newChat();
    else if (s.cmd === '/help') shortcutsDialog();
    else if (s.cmd === '/clear') emptyConv();
    else if (s.cmd === '/model') {
        const id = matchModel(arg);
        if (id) await setControl('model', id, 'Model'); else say(`No model "${s.arg}". The list is under Model.`);
    } else if (s.cmd === '/effort') {
        if ((CHAT.efforts || ['auto', 'low', 'medium', 'high', 'max']).includes(arg)) await setControl('effort', arg, 'Effort'); else say('Effort is auto, low, medium, high or max.');
    } else if (s.cmd === '/mode') {
        if (MODE_WORDS[arg]) await setControl('mode', MODE_WORDS[arg], 'Mode'); else say('Mode is auto (autonomous), ask or plan.');
    }
    return true;
}

function emptyConv() {
    if (!CHAT.conv?.id) { say('This chat is already empty.'); return; }
    dialog('Empty this chat?', [el('p', { text: 'Its messages are deleted from the household host; its model, effort and mode stay. Work it queued keeps running. This cannot be undone.' })],
        [el('button', { class: 'btn btn--danger', type: 'button', text: 'Empty it', onclick: async () => {
            try { await chatApi({ action: 'empty', id: CHAT.conv.id }); } catch (err) { say(err.message); }
            cq('chat-dialog').close();
            CHAT.conv.messages = [];
            CHAT.conv.usage = null;
            drawLog();
            say('This chat is empty.');
        } })]);
}

function shortcutsDialog() {
    dialog('Commands and shortcuts', [
        el('h3', { class: 'card__sub', text: 'Commands' }),
        el('dl', { class: 'chat__keys' }, ...SLASH.flatMap((c) => [el('dt', {}, el('kbd', { text: c.cmd + (c.arg ? ' …' : '') })), el('dd', { text: c.help })])),
        el('h3', { class: 'card__sub', text: 'Keys' }),
        el('dl', { class: 'chat__keys' }, ...SHORTCUTS.flatMap(([k, v]) => [el('dt', {}, el('kbd', { text: k })), el('dd', { text: v })]))], []);
}

// ---- the popup: slash commands and @-mentions, a listbox the arrow keys move through

function closePopup() {
    ctl.popup = null;
    ctl.items = [];
    ctl.active = -1;
    cq('chat-popup').hidden = true;
    cq('chat-input').removeAttribute('aria-activedescendant');
}

function drawPopup() {
    const box = cq('chat-popup');
    if (!ctl.items.length) { closePopup(); return; }
    box.replaceChildren(...ctl.items.map((it, i) => el('li', {
        id: `chat-opt-${i}`, role: 'option', class: 'chat__opt', 'aria-selected': String(i === ctl.active),
        onmousedown: (e) => { e.preventDefault(); acceptPopup(i); },
    }, el('strong', { text: it.text }), it.help ? el('span', { text: ` ${it.help}` }) : null)));
    box.hidden = false;
    if (ctl.active >= 0) cq('chat-input').setAttribute('aria-activedescendant', `chat-opt-${ctl.active}`);
}

async function updatePopup() {
    const input = cq('chat-input');
    const want = popupFor(input.value, input.selectionStart);
    if (!want) { closePopup(); return; }
    ctl.popup = want;
    if (want.kind === 'slash') {
        ctl.items = SLASH.filter((c) => c.cmd.startsWith(want.query)).map((c) => ({ text: c.cmd, help: c.help, insert: c.cmd + (c.arg ? ' ' : '') }));
        ctl.active = ctl.items.length ? 0 : -1;
        drawPopup();
        return;
    }
    clearTimeout(ctl.timer);
    ctl.timer = setTimeout(async () => {
        const data = await chatApi({ action: 'mentions', query: want.query }).catch(() => ({ options: [] }));
        if (ctl.popup !== want) return;
        ctl.items = data.options.map((o) => ({ text: `@${o.kind}:${o.name}`, help: o.label, insert: `@${o.kind}:${o.name} ` }));
        ctl.active = ctl.items.length ? 0 : -1;
        drawPopup();
        if (ctl.items.length) say(`${ctl.items.length} suggestions: up and down to choose, Enter to add.`);
    }, 150);
}

function acceptPopup(i) {
    const it = ctl.items[i];
    const input = cq('chat-input');
    if (!it || !ctl.popup) return;
    const caret = input.selectionStart;
    input.value = input.value.slice(0, ctl.popup.start) + it.insert + input.value.slice(caret);
    const at = ctl.popup.start + it.insert.length;
    input.setSelectionRange(at, at);
    closePopup();
    autoGrow();
    input.focus();
}

// Keys while the popup is open: it takes them before the composer does. True when used.
function popupKey(e) {
    if (!ctl.popup || !ctl.items.length) return false;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        ctl.active = (ctl.active + (e.key === 'ArrowDown' ? 1 : -1) + ctl.items.length) % ctl.items.length;
        drawPopup();
    } else if (e.key === 'Enter' || e.key === 'Tab') {
        acceptPopup(Math.max(0, ctl.active));
    } else if (e.key === 'Escape') {
        closePopup();
    } else {
        return false;
    }
    e.preventDefault();
    return true;
}

// ---- pasting: a screenshot becomes an attachment; a very long text (a log) too

async function onPaste(e) {
    const files = [...(e.clipboardData?.files || [])];
    const text = e.clipboardData?.getData('text/plain') || '';
    if (!files.length && text.length < 4000) return;
    e.preventDefault();
    const items = files.length ? files : [new File([text], 'pasted.log', { type: 'text/plain' })];
    const errors = [];
    for (const file of items) {
        if (CHAT.pending.length >= CHAT.limits.attachments) { errors.push(`At most ${CHAT.limits.attachments} files per message.`); break; }
        const why = attachmentError(file, CHAT.limits.attachment_bytes);
        const item = why || await readAttachment(file);
        if (typeof item === 'string') errors.push(item); else CHAT.pending.push(item);
    }
    cq('chat-file-error').textContent = errors.join(' ');
    drawPending();
    if (!errors.length) say(files.length ? 'Pasted as an attachment.' : 'The long text was attached as pasted.log.');
}

// ---- keyboard shortcuts in the Chat

function chatShortcut(e) {
    if (cq('chat-view').hidden || cq('chat-dialog').open) return;
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '');
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); document.body.dataset.chatPane = 'list'; cq('chat-search').focus(); }
    else if (e.altKey && e.key.toLowerCase() === 'n') { e.preventDefault(); newChat(); }
    else if (!typing && e.key === '/') { e.preventDefault(); cq('chat-input').focus(); }
    else if (!typing && e.key === '?') { e.preventDefault(); shortcutsDialog(); }
}

function initChatControls() {
    cq('chat-model').addEventListener('change', (e) => setControl('model', e.target.value, 'Model'));
    cq('chat-effort').addEventListener('change', (e) => setControl('effort', e.target.value, 'Effort'));
    cq('chat-mode').addEventListener('change', (e) => setControl('mode', e.target.value, 'Mode'));
    cq('chat-input').addEventListener('input', () => void updatePopup());
    cq('chat-input').addEventListener('blur', () => setTimeout(closePopup, 150));
    cq('chat-input').addEventListener('paste', (e) => void onPaste(e));
    document.addEventListener('keydown', chatShortcut);
    cq('chat-toggle-terminal').addEventListener('click', () => togglePanel('terminal'));
    cq('chat-toggle-browser').addEventListener('click', () => togglePanel('browser'));
    drawControls();
}

if (typeof document !== 'undefined' && document.addEventListener) document.addEventListener('DOMContentLoaded', initChatControls);
if (typeof module !== 'undefined') module.exports = { parseSlash, tokenAt, popupFor, usageText, shortNum, SLASH };
