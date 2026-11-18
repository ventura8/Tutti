// Tutti's Chat tab (docs/CHAT.md): the default view. Conversations per user, answered on the household host and streamed as
// JSON lines. Everything from the server or a model is drawn with DOM nodes and textContent: the Markdown parser below builds a
// plain tree first (tested in node), so no message can ever become markup.
'use strict';

// ---- Markdown, safely: text -> a tree of {t, c} nodes (no DOM), then DOM nodes with textContent only

const SAFE_URL = /^(https?:\/\/|mailto:|#)/i;

function mdInline(text) {
    const out = [];
    const re = /(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)|\*\*([^*]+)\*\*|__([^_]+)__|\*([^*\s][^*]*)\*|_([^_\s][^_]*)_|\[([^\]]+)\]\(([^)\s]+)\)/g;
    let at = 0;
    for (let m = re.exec(text); m; m = re.exec(text)) {
        if (m.index > at) out.push(text.slice(at, m.index));
        if (m[1]) out.push({ t: 'code', c: [m[2].trim()] });
        else if (m[3] || m[4]) out.push({ t: 'strong', c: mdInline(m[3] || m[4]) });
        else if (m[5] || m[6]) out.push({ t: 'em', c: mdInline(m[5] || m[6]) });
        else if (SAFE_URL.test(m[8])) out.push({ t: 'a', href: m[8], c: mdInline(m[7]) });
        else out.push(m[0]);   // a link to javascript: or data: stays plain text
        at = re.lastIndex;
    }
    if (at < text.length) out.push(text.slice(at));
    return out;
}

function mdList(lines, i, ordered) {
    const items = [];
    const marker = ordered ? /^\s*\d+[.)]\s+(.*)$/ : /^\s*[-*+]\s+(.*)$/;
    while (i < lines.length && marker.test(lines[i])) {
        items.push({ t: 'li', c: mdInline(lines[i].match(marker)[1]) });
        i += 1;
    }
    return [{ t: ordered ? 'ol' : 'ul', c: items }, i];
}

// A line that starts another block: the same fence pattern as mdBlock's, so a line like "```js foo" stays paragraph text.
const FENCE = /^\s*```\s*([\w+-]*)\s*$/;
const BLOCK_START = /^(#{1,6}\s|\s*[-*+]\s|\s*\d+[.)]\s|\s*>)/;
const isBlockStart = (line) => FENCE.test(line) || BLOCK_START.test(line);

function mdBlock(lines, i) {
    const line = lines[i];
    const fence = line.match(FENCE);
    if (fence) {
        const body = [];
        let j = i + 1;
        while (j < lines.length && !/^\s*```\s*$/.test(lines[j])) body.push(lines[j++]);
        return [{ t: 'pre', lang: fence[1] || '', c: [body.join('\n')] }, j + 1];
    }
    const head = line.match(/^(#{1,6})\s+(.*)$/);
    if (head) return [{ t: 'h', level: Math.min(head[1].length + 2, 6), c: mdInline(head[2]) }, i + 1];
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) return [{ t: 'hr', c: [] }, i + 1];
    if (/^\s*[-*+]\s+/.test(line)) return mdList(lines, i, false);
    if (/^\s*\d+[.)]\s+/.test(line)) return mdList(lines, i, true);
    if (/^\s*>/.test(line)) {
        const body = [];
        let j = i;
        while (j < lines.length && /^\s*>/.test(lines[j])) body.push(lines[j++].replace(/^\s*>\s?/, ''));
        return [{ t: 'blockquote', c: mdParse(body.join('\n')) }, j];
    }
    const para = [];
    let j = i;
    while (j < lines.length && lines[j].trim() && !isBlockStart(lines[j])) para.push(lines[j++]);
    const kids = [];
    para.forEach((p, k) => { if (k) kids.push({ t: 'br', c: [] }); kids.push(...mdInline(p)); });
    return [{ t: 'p', c: kids }, Math.max(j, i + 1)];
}

function mdParse(text) {
    const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let i = 0;
    while (i < lines.length) {
        if (!lines[i].trim()) { i += 1; continue; }
        const [node, next] = mdBlock(lines, i);
        out.push(node);
        i = next;
    }
    return out;
}

const MD_TAGS = { p: 'p', strong: 'strong', em: 'em', code: 'code', ul: 'ul', ol: 'ol', li: 'li', blockquote: 'blockquote', hr: 'hr', br: 'br' };

function mdNode(n) {
    if (typeof n === 'string') return document.createTextNode(n);
    if (n.t === 'pre') return codeBlock(n.c[0], n.lang);
    let node;
    if (n.t === 'h') node = document.createElement(`h${n.level}`);
    else if (n.t === 'a') {
        node = document.createElement('a');
        node.href = n.href;
        if (!n.href.startsWith('#')) { node.target = '_blank'; node.rel = 'noopener noreferrer'; }
    } else node = document.createElement(MD_TAGS[n.t] || 'span');
    for (const kid of n.c) node.append(mdNode(kid));
    return node;
}

function mdRender(text) {
    const frag = document.createDocumentFragment();
    for (const n of mdParse(text)) frag.append(mdNode(n));
    return frag;
}

function codeBlock(code, lang) {
    const pre = el('pre', { class: 'md__pre', tabindex: 0, 'aria-label': lang ? `Code, ${lang}` : 'Code' }, el('code', { text: code }));
    const copy = el('button', { class: 'btn btn--text md__copy', type: 'button', text: 'Copy', 'aria-label': 'Copy the code' });
    copy.addEventListener('click', () => copyText(code, copy));
    return el('div', { class: 'md__code' }, el('div', { class: 'md__codebar' }, el('span', { text: lang || 'code' }), copy), pre);
}

// ---- pure helpers (node tests)

// What a key press in the composer does: send, stop, edit the last message, or nothing (the browser's default).
function chatKey(e, state) {
    if (e.isComposing || e.keyCode === 229) return null;   // an IME is composing: Enter picks a candidate
    if (e.key === 'Escape') return state.streaming ? 'stop' : null;
    if (e.key === 'Enter' && !e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey) return 'send';
    if (e.key === 'ArrowUp' && !state.text && state.canEdit) return 'edit';
    return null;
}

// Whether the log is scrolled to its end (within `slack` px): new text then keeps it there.
const atBottom = (box, slack = 80) => box.scrollHeight - box.scrollTop - box.clientHeight <= slack;

// A file the composer may attach: text only, within the size limit. Returns an error in plain words, or ''.
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp', 'image/gif'];
const IMAGE_LIMIT = 1024 * 1024;
const TEXT_NAME = /\.(txt|log|md|json|ya?ml|csv|tsv|xml|ini|toml|cfg|conf|py|js|ts|tsx|jsx|sh|go|rs|java|kt|c|h|cpp|diff|patch)$/i;

function attachmentError(file, limit) {
    if (IMAGE_TYPES.includes(file.type)) {
        return file.size > IMAGE_LIMIT ? `"${file.name}" is ${Math.ceil(file.size / 1024)} KB; images are limited to 1024 KB. Crop it or take a smaller screenshot.` : '';
    }
    const textual = file.type.startsWith('text/') || TEXT_NAME.test(file.name);
    if (!textual) return `"${file.name}" is not a text file or an image. Attach text, logs or screenshots (PNG, JPEG, WebP, GIF).`;
    if (file.size > limit) return `"${file.name}" is ${Math.ceil(file.size / 1024)} KB; the limit is ${Math.floor(limit / 1024)} KB. Attach the part that matters.`;
    return '';
}

// One NDJSON chunk's complete lines as events, and the rest to keep for the next chunk.
function ndjson(buffer) {
    const parts = buffer.split('\n');
    const rest = parts.pop();
    const events = [];
    for (const p of parts) { if (p.trim()) { try { events.push(JSON.parse(p)); } catch { /* a broken line is skipped */ } } }
    return [events, rest];
}

// ---- state

const CHAT = {
    list: [], suggestions: [], role: 'admin', limits: { attachment_bytes: 32768, attachments: 3, text: 8000 }, host: '',
    conv: null, query: '', loading: true, failed: '', streaming: null, controller: null, pending: [], stick: true, unseen: 0,
};
const CHAT_TOKEN = (typeof document !== 'undefined' && document.querySelector('meta[name="tutti-token"]')?.content) || '';
const cq = (id) => document.getElementById(id);
const reduceMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
const timeFmt = typeof Intl !== 'undefined' ? new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }) : null;
const dayFmt = typeof Intl !== 'undefined' ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : null;

async function chatApi(body, signal) {
    const res = await fetch('/api/chat', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': CHAT_TOKEN }, body: JSON.stringify(body), signal,
    });
    if (res.headers.get('Content-Type')?.startsWith('application/x-ndjson')) return res;
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || 'The Tutti service did not answer. It reconnects by itself; try again in a moment.');
    return data;
}

function say(text) {   // the screen reader's announcement (a polite live region)
    const box = cq('chat-announce');
    box.textContent = '';
    setTimeout(() => { box.textContent = text; }, 50);
}

function copyText(text, button) {
    const done = () => { const was = button.textContent; button.textContent = 'Copied'; setTimeout(() => { button.textContent = was; }, 1500); };
    navigator.clipboard?.writeText(text).then(done, () => say('Copying is blocked by the browser: select the text and copy it.'));
}

// ---- the conversation list

async function loadList() {
    try {
        const data = await chatApi({ action: 'list', query: CHAT.query });
        Object.assign(CHAT, { list: data.conversations, suggestions: data.suggestions, role: data.role, limits: data.limits, host: data.host || '', failed: '',
            models: data.models || [], defaults: data.defaults || {}, modes: data.modes || {}, efforts: data.efforts || [],
            compactDefault: data.compact_default || 60, mainAgent: data.main_agent || null });
        if (typeof drawControls === 'function') drawControls();
        drawMainAgent();
    } catch (err) {
        CHAT.failed = err.message;
    }
    CHAT.loading = false;
    drawList();
    drawPrivacy();
    if (!CHAT.conv) drawLog();
}

function drawMainAgent() {
    const node = cq('chat-main-agent');
    if (!node) return;
    const m = CHAT.mainAgent;
    node.textContent = m?.label ? `main agent now: ${m.label}` : (m?.name ? `main agent now: ${m.name}` : '');
}

function drawList() {
    const box = cq('chat-list');
    const note = cq('chat-list-note');
    if (CHAT.loading) { note.textContent = 'Loading your conversations…'; box.replaceChildren(); return; }
    if (CHAT.failed && !CHAT.list.length) {
        note.replaceChildren(`Your conversations did not load: ${CHAT.failed} `,
            el('button', { class: 'btn btn--text', type: 'button', text: 'Try again', onclick: () => { CHAT.loading = true; drawList(); void loadList(); } }));
        box.replaceChildren();
        return;
    }
    note.textContent = CHAT.list.length ? '' : CHAT.query ? `No conversation mentions "${CHAT.query}".` : 'No conversations yet. Ask something to start one.';
    box.replaceChildren(...CHAT.list.map((c) => el('li', {},
        el('button', {
            class: 'chat__item', type: 'button', 'aria-current': CHAT.conv?.id === c.id ? 'true' : null, onclick: () => openConv(c.id),
        }, el('span', { class: 'chat__itemtitle', text: c.title }),
        el('span', { class: 'chat__itemmeta', text: `${c.updated ? relative(c.updated) : ''}${c.preview ? ' · ' + c.preview : ''}` })))));
}

function drawPrivacy() {
    const where = CHAT.host ? `the household host (${CHAT.host})` : 'the household host';
    cq('chat-privacy').textContent = `Your conversations are kept on ${where}, in Tutti's state folder, in a file only Tutti's own ` +
        'account can read. Other members never see them; admins see only the actions the chat took (in the audit log), never your ' +
        'messages. Models of the household answer; nothing is sent elsewhere except to the account that answers.';
    cq('chat-activity-open').hidden = CHAT.role !== 'admin';
}

async function openConv(id, focus = true) {
    if (CHAT.streaming) stopAnswer();
    try {
        const got = await chatApi({ action: 'get', id });
        CHAT.conv = got.conversation;
        CHAT.settings = got.settings;
    } catch (err) {
        say(err.message);
        CHAT.conv = null;
        await loadList();
        return;
    }
    CHAT.stick = true;
    document.body.dataset.chatPane = 'conv';
    drawList();
    drawLog();
    const live = CHAT.conv.messages.find((m) => m.status === 'streaming');
    if (live) void follow(live.id);
    if (focus) cq('chat-input').focus();
}

function newChat() {
    if (CHAT.streaming) stopAnswer();
    CHAT.conv = null;
    CHAT.settings = null;
    CHAT.pending = [];
    document.body.dataset.chatPane = 'conv';
    drawList();
    drawLog();
    cq('chat-input').focus();
}

// ---- the messages

function drawLog() {
    const log = cq('chat-log');
    const msgs = CHAT.conv?.messages || [];
    cq('chat-title').textContent = CHAT.conv?.title || 'New chat';
    cq('chat-conv-actions').hidden = !CHAT.conv;
    if (typeof drawControls === 'function') drawControls();
    if (!msgs.length) {
        log.replaceChildren(emptyState());
        return;
    }
    const lastUser = [...msgs].reverse().find((m) => m.role === 'user');
    const lastAnswer = msgs[msgs.length - 1].role === 'assistant' ? msgs[msgs.length - 1] : null;
    const upto = CHAT.conv.summary?.upto;
    log.replaceChildren(...msgs.flatMap((m) => {
        const node = chatMessage(m, { editable: m.role === 'user' && !CHAT.streaming, last: m === lastUser,
            retryable: m === lastAnswer && !CHAT.streaming && !(m.cards || []).length });
        return m.id === upto ? [node, el('p', { class: 'chat__compacted', role: 'note', text: 'Earlier messages above are summarised for the model to save context.' })] : [node];
    }));
    scrollEnd(true);
}

function emptyState() {
    if (CHAT.loading) return el('div', { class: 'chat__empty' }, el('p', { class: 'chat__lead', text: 'Loading…' }));
    const tips = CHAT.suggestions.map((s) => el('button', { class: 'chip chat__suggest', type: 'button', text: s, onclick: () => send(s) }));
    return el('div', { class: 'chat__empty' },
        el('h3', { class: 'chat__lead', text: 'Ask the household' }),
        el('p', { class: 'chat__sub', text: 'What is running, how much quota is left, why a machine is idle. Or queue work: "add task in <project>: what to change". It answers from the household host, so it works while your own computer is off.' }),
        tips.length ? el('div', { class: 'chat__suggestions', role: 'group', 'aria-label': 'Suggested questions' }, ...tips) : null,
        CHAT.failed ? el('p', { class: 'chat__error', role: 'alert', text: `The chat is not reachable right now: ${CHAT.failed}` }) : null);
}

function chatMessage(m, opts) {
    const mine = m.role === 'user';
    const at = m.at ? new Date(m.at) : null;
    const status = statusText(m);
    const body = el('div', { class: 'msg__body md' });
    if (mine) body.append(...String(m.text || '').split('\n').flatMap((line, i) => (i ? [el('br'), line] : [line])));
    else if (m.text) body.append(mdRender(m.text));
    else if (m.status === 'streaming') body.append(el('span', { class: 'typing', role: 'img', 'aria-label': 'Tutti is writing' }, el('i'), el('i'), el('i')));
    const node = el('article', { class: `msg msg--${mine ? 'user' : 'bot'}`, 'data-id': m.id, 'data-status': m.status || '',
        'aria-label': `${mine ? 'You' : 'Tutti'}${at ? ', ' + timeFmt.format(at) : ''}` },
    el('header', { class: 'msg__meta' },
        el('span', { class: 'msg__who', text: mine ? 'You' : 'Tutti' }),
        at ? el('time', { datetime: m.at, title: dayFmt.format(at), text: timeFmt.format(at) }) : null,
        !mine && m.model ? el('span', { class: 'msg__model', title: 'The model that answered', text: m.model }) : null,
        !mine && m.fallback ? el('span', { class: 'msg__fallback', text: m.fallback }) : null,
        status ? el('span', { class: 'msg__status', 'data-tone': m.status, text: status }) : null),
    body,
    (m.attachments || []).length ? el('ul', { class: 'msg__files', 'aria-label': 'Attached' }, ...m.attachments.map((a) => el('li', { class: 'chip', text: `${a.name} · ${Math.ceil(a.size / 1024)} KB` }))) : null,
    ...(m.cards || []).map((c) => actionCard(m, c)),
    m.note ? el('p', { class: 'msg__note', role: 'note', text: m.note }) : null,
    (m.try || []).length ? el('div', { class: 'chat__suggestions', role: 'group', 'aria-label': 'Do it' },
        ...m.try.map((t) => el('button', { class: 'chip chat__suggest', type: 'button', text: t, title: 'Send this request', onclick: () => send(t) }))) : null,
    messageActions(m, opts));
    return node;
}

function statusText(m) {
    return { sending: 'Sending…', unsent: 'Not sent', streaming: '', stopped: 'Stopped', failed: 'Failed' }[m.status] || (m.edited ? 'Edited' : '');
}

function messageActions(m, opts) {
    const acts = [];
    if (m.role === 'assistant' && m.text) acts.push(el('button', { class: 'btn btn--text', type: 'button', text: 'Copy', title: 'Copy this answer', onclick: (e) => copyText(m.text, e.currentTarget) }));
    if (opts.retryable) acts.push(el('button', { class: 'btn btn--text', type: 'button', text: 'Regenerate', title: 'Answer this again', onclick: () => retry(m.id) }));
    if (!String(m.id).startsWith('local-') && m.status !== 'streaming' && !CHAT.streaming && CHAT.conv?.id) {
        acts.push(el('button', { class: 'btn btn--text', type: 'button', text: 'Branch', title: 'A new chat with the messages up to here', onclick: () => branchFrom(m) }));
    }
    if (m.status === 'unsent') acts.push(el('button', { class: 'btn btn--tonal', type: 'button', text: 'Send again', onclick: () => resend(m) }));
    if (opts.editable && !String(m.id).startsWith('local-')) acts.push(el('button', { class: 'btn btn--text', type: 'button', text: 'Edit', title: 'Edit and resend: the messages after it are replaced', onclick: () => startEdit(m) }));
    return acts.length ? el('footer', { class: 'msg__actions' }, ...acts) : null;
}

// ---- action cards: what the chat did, with Undo, and the work's status from the dashboard's own poll (app.js keeps ui.last)

function workStatus(ref) {
    const snap = typeof ui !== 'undefined' ? ui.last : null;
    if (!ref || !snap) return null;
    const job = (snap.jobs?.items || []).find((j) => String(j.id) === ref || String(j.id).split(':').pop() === ref);
    if (job) {
        const where = job.machine || job.worker || 'the host';
        const took = job.progress?.elapsed_seconds ? ` · ${duration(job.progress.elapsed_seconds)}` : '';
        return { text: `${job.status === 'running' ? 'Running' : job.status} on ${where}${took}`, job };
    }
    const queued = (snap.queue?.groups || []).flatMap((g) => g.items || []).find((i) => i.id === ref);
    if (queued) return { text: `Queued${queued.why ? ' · waits: ' + queued.why : ''}` };
    return null;
}

function actionCard(m, c) {
    if (c.steps && typeof planCard === 'function') return planCard(m, c);
    const live = c.state === 'done' ? workStatus(c.ref) : null;
    const tone = { done: 'ok', failed: 'bad', refused: 'warn', undone: 'idle', proposed: 'accent', cancelled: 'idle' }[c.state] || 'idle';
    const title = { done: c.label, failed: `Not done: ${c.label}`, refused: `Not allowed: ${c.label}`, undone: `Undone: ${c.label}`,
        proposed: `Confirm? ${c.label}`, cancelled: `Cancelled: ${c.label}` }[c.state] || c.label;
    const card = el('section', { class: 'act', 'data-tone': tone, 'data-card': c.id, 'aria-label': title },
        el('p', { class: 'act__title' }, el('span', { class: 'chip', 'data-tone': tone, text: { done: 'Done', failed: 'Failed', refused: 'Not allowed', undone: 'Undone', proposed: 'Needs your OK', cancelled: 'Cancelled' }[c.state] || c.state }), ' ', title),
        c.text ? el('p', { class: 'act__text', text: c.text }) : null,
        live ? el('p', { class: 'act__live' }, live.text, ' ', el('a', { class: 'link', href: '#dashboard', text: 'Open in the dashboard', onclick: () => openWork(live.job) })) : null,
        c.ref && !live && c.state === 'done' ? el('p', { class: 'act__live', text: `Id ${c.ref}: on the Dashboard tab under Queued or Running jobs.` }) : null);
    if (c.state === 'proposed') {
        card.append(el('div', { class: 'act__btns' },
            el('button', { class: 'btn btn--filled', type: 'button', text: 'Confirm', onclick: (e) => confirmCard(m, c, true, e.currentTarget) }),
            el('button', { class: 'btn btn--text', type: 'button', text: 'Cancel', onclick: (e) => confirmCard(m, c, false, e.currentTarget) })));
    }
    if (c.state === 'done' && c.undo) {
        card.append(el('div', { class: 'act__btns' }, el('button', { class: 'btn btn--tonal', type: 'button', text: c.tool === 'control' ? 'Undo' : 'Undo (remove it)', onclick: (e) => undo(m, c, e.currentTarget) })));
    }
    return card;
}

function openWork(job) {
    if (job && typeof openJob === 'function') setTimeout(() => openJob(job), 50);
}

async function confirmCard(m, c, yes, button) {   // a model's proposal: nothing runs until the user says so
    button.disabled = true;
    try {
        const data = await chatApi({ action: 'confirm', conversation: CHAT.conv.id, message: m.id, card: c.id, yes });
        Object.assign(c, data.card);
        document.querySelector(`[data-card="${c.id}"]`)?.replaceWith(actionCard(m, c));
        say(yes ? `${data.card.state === 'done' ? 'Done' : 'Not done'}: ${c.label}` : `Cancelled: ${c.label}`);
    } catch (err) {
        button.disabled = false;
        say(err.message);
    }
}

async function undo(m, c, button) {
    button.disabled = true;
    try {
        const data = await chatApi({ action: 'undo', conversation: CHAT.conv.id, message: m.id, card: c.id });
        CHAT.conv = data.conversation;
        drawLog();
        say(`Undone: ${c.label}`);
    } catch (err) {
        button.disabled = false;
        say(err.message);
        button.after(el('p', { class: 'chat__error', role: 'alert', text: err.message }));
    }
}

function refreshCards() {
    if (!CHAT.conv || document.hidden || cq('chat-view').hidden) return;
    for (const m of CHAT.conv.messages) {
        for (const c of m.cards || []) {
            const old = document.querySelector(`[data-card="${c.id}"]`);
            if (old && c.state === 'done' && c.ref) old.replaceWith(actionCard(m, c));
        }
    }
}

// ---- sending and streaming

function composerText() { return cq('chat-input').value.replace(/\s+$/, ''); }

async function send(text, extra = {}) {
    text = (text ?? composerText()).trim();
    if ((!text && !CHAT.pending.length) || CHAT.streaming) return;
    if (text.startsWith('/') && typeof runSlash === 'function' && await runSlash(text)) {
        cq('chat-input').value = '';
        autoGrow();
        return;
    }
    const attachments = CHAT.pending;
    if (!CHAT.conv) CHAT.conv = { id: null, title: '', messages: [] };
    if (extra.edit) CHAT.conv.messages = CHAT.conv.messages.slice(0, CHAT.conv.messages.findIndex((x) => x.id === extra.edit));
    const local = { id: `local-${Date.now()}`, role: 'user', text, attachments: attachments.map((a) => ({ name: a.name, size: a.size || (a.text || '').length })), at: new Date().toISOString(), status: 'sending' };
    CHAT.conv.messages.push(local);
    cq('chat-input').value = '';
    CHAT.pending = [];
    drawPending();
    autoGrow();
    CHAT.stick = true;
    drawLog();
    const picks = typeof currentSettings === 'function' ? currentSettings() : {};
    await stream({ action: 'send', conversation: CHAT.conv.id, text, attachments, ...(CHAT.conv.id ? {} : picks), ...extra }, local);
}

function resend(m) {
    CHAT.conv.messages = CHAT.conv.messages.filter((x) => x !== m);
    void send(m.text);
}

async function retry(id) {
    if (CHAT.streaming) return;
    CHAT.conv.messages = CHAT.conv.messages.filter((x) => x.id !== id);
    drawLog();
    await stream({ action: 'send', conversation: CHAT.conv.id, retry: id }, null);
}

async function branchFrom(m) {
    try {
        const data = await chatApi({ action: 'branch', conversation: CHAT.conv.id, message: m.id });
        await loadList();
        await openConv(data.conversation.id);
        say('A new chat starts from that message. The original is unchanged.');
    } catch (err) {
        say(err.message);
    }
}

function startEdit(m) {
    const box = cq('chat-input');
    box.value = m.text;
    CHAT.editing = m.id;
    cq('chat-editing').hidden = false;
    autoGrow();
    box.focus();
    box.setSelectionRange(box.value.length, box.value.length);
}

function cancelEdit() {
    CHAT.editing = null;
    cq('chat-editing').hidden = true;
}

async function follow(id) {
    const msg = CHAT.conv.messages.find((m) => m.id === id);
    await stream({ action: 'follow', message: id }, null, msg, true);
}

async function stream(body, local, known = null, following = false) {
    const controller = new AbortController();
    CHAT.controller = controller;
    CHAT.streaming = known || { id: null };
    setBusy(true);
    let answer = known;
    try {
        const res = await chatApi(body, controller.signal);
        if (!res.body) throw new Error('This browser cannot read a streamed answer; reload the page.');
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            let events;
            [events, buf] = ndjson(buf + dec.decode(value, { stream: true }));
            for (const e of events) answer = onEvent(e, local, answer);
        }
    } catch (err) {
        if (following && err.name !== 'AbortError') {   // the answer finished meanwhile: show it as saved
            const fresh = await chatApi({ action: 'get', id: CHAT.conv.id }).catch(() => null);
            if (fresh) CHAT.conv = fresh.conversation;
        } else if (err.name !== 'AbortError') failSend(local, answer, err);
    } finally {
        CHAT.streaming = null;
        CHAT.controller = null;
        setBusy(false);
        drawLog();
        void loadList();
    }
}

function failSend(local, answer, err) {
    if (local && local.status === 'sending') {
        local.status = 'unsent';
        say(`Your message was not sent: ${err.message}`);
    } else if (answer) {
        answer.status = 'failed';
        answer.note = 'The connection to the household host was lost while answering. Reload the chat to see the answer: it is kept there.';
        say(answer.note);
    }
}

function onEvent(e, local, answer) {
    if (e.type === 'start') {
        if (local) Object.assign(local, e.prompt, { status: 'sent' });
        if (!CHAT.conv.id) CHAT.conv.id = e.conversation;
        if (!CHAT.conv.title && e.prompt?.text) CHAT.conv.title = e.prompt.text.replace(/\s+/g, ' ').slice(0, 80);
        answer = { ...e.message };
        CHAT.streaming = answer;
        CHAT.conv.messages.push(answer);
        drawLog();
    } else if (answer && e.type === 'delta') {
        answer.text += e.text;
        paint(answer);
    } else if (answer && e.type === 'model') {
        answer.model = e.model;
    } else if (e.type === 'usage') {
        CHAT.conv.usage = e.total;
        if (typeof drawUsage === 'function') drawUsage();
    } else if (e.type === 'compacted') {
        CHAT.conv.summary = { upto: e.upto };
        say(e.text);
    } else if (answer && e.type === 'note') {
        answer.note = e.text;
        paint(answer);
    } else if (answer && e.type === 'replace') {
        answer.text = e.text;
        paint(answer);
    } else if (answer && e.type === 'fallback') {
        answer.fallback = e.text;
        paint(answer);
    } else if (answer && e.type === 'card') {
        answer.cards = [...(answer.cards || []), e.card];
        paint(answer);
    } else if (answer && e.type === 'done') {
        Object.assign(answer, e.message);
        const first = (answer.cards?.[0]?.label ? answer.cards[0].label + '. ' : '') + answer.text;
        say(`Tutti answered${answer.model ? ' (' + answer.model + ')' : ''}: ${first.slice(0, 240)}`);
    }
    return answer;
}

let paintFrame = 0;
function paint(answer) {   // one repaint per frame while text streams in
    if (paintFrame) return;
    paintFrame = requestAnimationFrame(() => {
        paintFrame = 0;
        const old = document.querySelector(`.msg[data-id="${answer.id}"]`);
        const node = chatMessage(answer, { editable: false, retryable: false });
        if (old) old.replaceWith(node); else cq('chat-log').append(node);
        scrollEnd(false);
    });
}

function stopAnswer() {
    const id = CHAT.streaming?.id;
    if (id) void chatApi({ action: 'stop', message: id }).catch(() => null);
    CHAT.controller?.abort();
    say('Stopped.');
}

function setBusy(on) {
    cq('chat-send').hidden = on;
    cq('chat-stop').hidden = !on;
    cq('chat-log').setAttribute('aria-busy', String(on));
    if (!on && document.activeElement === document.body) cq('chat-input').focus();
}

// ---- scrolling: follow new text while at the end; otherwise offer "Jump to latest"

function scrollEnd(force) {
    const log = cq('chat-log');
    if (force || CHAT.stick) {
        log.scrollTo({ top: log.scrollHeight, behavior: 'auto' });
        CHAT.unseen = 0;
    } else {
        CHAT.unseen += 1;
    }
    cq('chat-jump').hidden = CHAT.stick || force;
}

function onScroll() {
    CHAT.stick = atBottom(cq('chat-log'));
    if (CHAT.stick) cq('chat-jump').hidden = true;
}

function jumpLatest() {
    const log = cq('chat-log');
    CHAT.stick = true;
    log.scrollTo({ top: log.scrollHeight, behavior: reduceMotion() ? 'auto' : 'smooth' });
    cq('chat-jump').hidden = true;
    cq('chat-input').focus();
}

// ---- the composer

function autoGrow() {
    const box = cq('chat-input');
    box.style.height = 'auto';
    box.style.height = `${Math.min(box.scrollHeight, 220)}px`;
}

function onKey(e) {
    if (typeof popupKey === 'function' && popupKey(e)) return;
    const action = chatKey(e, { streaming: Boolean(CHAT.streaming), text: cq('chat-input').value, canEdit: Boolean(lastUserMessage()) });
    if (!action) return;
    e.preventDefault();
    if (action === 'send') onSubmit(e);
    else if (action === 'stop') stopAnswer();
    else if (action === 'edit') startEdit(lastUserMessage());
}

function lastUserMessage() {
    return CHAT.streaming ? null : [...(CHAT.conv?.messages || [])].reverse().find((m) => m.role === 'user' && !String(m.id).startsWith('local-'));
}

function onSubmit(e) {
    e.preventDefault();
    if (CHAT.streaming) return;
    const edit = CHAT.editing;
    cancelEdit();
    void send(undefined, edit ? { edit } : {});
}

async function onFiles(e) {
    const err = [];
    for (const file of e.target.files) {
        if (CHAT.pending.length >= CHAT.limits.attachments) { err.push(`At most ${CHAT.limits.attachments} files per message.`); break; }
        const why = attachmentError(file, CHAT.limits.attachment_bytes);
        if (why) { err.push(why); continue; }
        const item = await readAttachment(file);
        if (typeof item === 'string') err.push(item); else CHAT.pending.push(item);
    }
    e.target.value = '';
    cq('chat-file-error').textContent = err.join(' ');
    drawPending();
}

// A file as an attachment: {name, text} or {name, type, data (base64), size} for an image; a string says why not.
async function readAttachment(file) {
    if (IMAGE_TYPES.includes(file.type)) {
        const bytes = new Uint8Array(await file.arrayBuffer());
        let bin = '';
        for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
        return { name: file.name || 'screenshot.png', type: file.type, data: btoa(bin), size: bytes.length };
    }
    const text = await file.text();
    if (text.includes('\u0000')) return `"${file.name}" is not a text file. Attach text or log snippets only.`;
    return { name: file.name, text, size: text.length };
}

function drawPending() {
    cq('chat-attachments').replaceChildren(...CHAT.pending.map((a, i) => el('span', { class: 'chip chat__file' },
        `${a.type ? 'Image: ' : ''}${a.name} · ${Math.ceil((a.size || 0) / 1024)} KB`,
        el('button', { class: 'chat__unattach', type: 'button', 'aria-label': `Remove ${a.name}`, text: '×', onclick: () => { CHAT.pending.splice(i, 1); drawPending(); cq('chat-input').focus(); } }))));
}

// ---- rename, delete, clear, activity (one dialog)

function dialog(title, body, actions) {
    const d = cq('chat-dialog');
    cq('chat-dialog-title').textContent = title;
    cq('chat-dialog-body').replaceChildren(...body);
    cq('chat-dialog-actions').replaceChildren(...actions, el('button', { class: 'btn btn--text', type: 'button', text: actions.length ? 'Cancel' : 'Close', onclick: () => d.close() }));
    d.showModal();
}

function renameConv() {
    const input = el('input', { class: 'setting__input chat__rename', id: 'chat-rename', value: CHAT.conv.title || '', maxlength: 80, autocomplete: 'off' });
    const save = async () => {
        try { await chatApi({ action: 'rename', id: CHAT.conv.id, title: input.value }); } catch (err) { cq('chat-dialog-error').textContent = err.message; return; }
        CHAT.conv.title = input.value.trim();
        cq('chat-dialog').close();
        drawLog();
        void loadList();
    };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); void save(); } });
    dialog('Rename this chat', [el('label', { for: 'chat-rename', text: 'Name' }), input, el('p', { class: 'chat__error', id: 'chat-dialog-error', role: 'alert' })],
        [el('button', { class: 'btn btn--filled', type: 'button', text: 'Save', onclick: save })]);
    input.select();
}

function deleteConv() {
    dialog('Delete this chat?', [el('p', { text: `"${CHAT.conv.title || 'New chat'}" and its messages are deleted from the household host. Work it queued is not affected. This cannot be undone.` })],
        [el('button', { class: 'btn btn--danger', type: 'button', text: 'Delete', onclick: async () => {
            try { await chatApi({ action: 'delete', id: CHAT.conv.id }); } catch (err) { say(err.message); }
            cq('chat-dialog').close();
            say('Chat deleted.');
            newChat();
            void loadList();
        } })]);
}

function clearHistory() {
    dialog('Clear your chat history?', [el('p', { text: 'Every one of your conversations is deleted from the household host. Work the chat queued keeps running, and the audit log keeps its record of what was done. This cannot be undone.' })],
        [el('button', { class: 'btn btn--danger', type: 'button', text: 'Clear history', onclick: async () => {
            try { await chatApi({ action: 'clear' }); } catch (err) { say(err.message); }
            cq('chat-dialog').close();
            say('Your chat history is cleared.');
            newChat();
            void loadList();
        } })]);
}

async function showActivity() {
    let rows;
    try {
        rows = (await chatApi({ action: 'activity' })).activity;
    } catch (err) {
        dialog('Household activity', [el('p', { class: 'chat__error', text: err.message })], []);
        return;
    }
    dialog('Household activity', [
        el('p', { class: 'chat__sub', text: 'What the chat did for each member, newest first. Their messages stay private.' }),
        rows.length ? el('ul', { class: 'chat__activity' }, ...rows.map((r) => el('li', {},
            el('time', { text: dayFmt.format(new Date(r.at * 1000)) }), ` ${r.by}: ${r.action}`))) : el('p', { text: 'The chat has not done anything yet.' })], []);
}

// ---- the views: Chat (the default), Dashboard, Settings, Users

function chatView() {
    const h = location.hash || '';
    const admin = typeof document !== 'undefined' && document.body?.dataset.role === 'admin';
    return h.startsWith('#settings') ? 'settings' : h.startsWith('#users') && admin ? 'users' : h.startsWith('#dashboard') ? 'dashboard' : 'chat';
}

function showChatView() {
    const on = chatView() === 'chat';
    cq('chat-view').hidden = !on;
    document.body.dataset.viewName = chatView();
    if (on && CHAT.loading) void loadList();
    if (on) requestAnimationFrame(measure);
}

function keyboardInset() {   // a phone's on-screen keyboard: keep the composer above it, hide the bottom tab bar meanwhile
    const vv = window.visualViewport;
    if (!vv) return;
    const inset = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
    document.documentElement.style.setProperty('--kb', `${inset}px`);
    document.body.classList.toggle('kbd-open', inset > 120);
}

function measure() {   // the app bar's, bottom tab bar's and composer's heights, for the chat's own height and the jump button
    const root = document.documentElement.style;
    const bar = document.querySelector('.appbar');
    const tabs = document.querySelector('.tabs');
    root.setProperty('--appbar-h', `${bar?.offsetHeight || 0}px`);
    const view = cq('chat-view');   // where the chat starts below the app bar and any banner
    if (!view.hidden) root.setProperty('--chat-top', `${Math.round(view.getBoundingClientRect().top + window.scrollY)}px`);
    root.setProperty('--tabs-h', `${tabs && getComputedStyle(tabs).position === 'fixed' ? tabs.offsetHeight : 0}px`);
    cq('chat-main')?.style.setProperty('--composer-h', `${cq('chat-form').offsetHeight}px`);
}

function initChat() {
    cq('chat-form').addEventListener('submit', onSubmit);
    cq('chat-input').addEventListener('keydown', onKey);
    cq('chat-input').addEventListener('input', autoGrow);
    cq('chat-stop').addEventListener('click', stopAnswer);
    cq('chat-new').addEventListener('click', newChat);
    cq('chat-attach').addEventListener('click', () => cq('chat-file').click());
    cq('chat-file').addEventListener('change', onFiles);
    cq('chat-log').addEventListener('scroll', onScroll, { passive: true });
    cq('chat-jump').addEventListener('click', jumpLatest);
    cq('chat-rename-btn').addEventListener('click', renameConv);
    cq('chat-delete-btn').addEventListener('click', deleteConv);
    cq('chat-clear').addEventListener('click', clearHistory);
    cq('chat-activity-open').addEventListener('click', showActivity);
    cq('chat-edit-cancel').addEventListener('click', () => { cancelEdit(); cq('chat-input').value = ''; autoGrow(); cq('chat-input').focus(); });
    cq('chat-back').addEventListener('click', () => { document.body.dataset.chatPane = 'list'; cq('chat-new').focus(); });
    let t = 0;
    cq('chat-search').addEventListener('input', (e) => { clearTimeout(t); t = setTimeout(() => { CHAT.query = e.target.value.trim(); void loadList(); }, 200); });
    window.addEventListener('hashchange', showChatView);
    window.visualViewport?.addEventListener('resize', keyboardInset);
    setInterval(refreshCards, 5000);
    if (window.ResizeObserver) {
        const ro = new ResizeObserver(measure);
        for (const n of [document.querySelector('.appbar'), cq('chat-form'), cq('banner'), cq('auth-banner')]) if (n) ro.observe(n);
    }
    window.addEventListener('resize', measure);
    measure();
    document.body.dataset.chatPane = 'list';
    showChatView();
}

if (typeof document !== 'undefined' && document.addEventListener) document.addEventListener('DOMContentLoaded', initChat);
if (typeof module !== 'undefined') module.exports = { mdParse, mdInline, chatKey, atBottom, attachmentError, ndjson, chatView };
