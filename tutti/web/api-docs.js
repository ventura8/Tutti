// The dashboard's own API page (/api-docs.html): /api/openapi.json drawn as one section per tag, each route with its method,
// who may call it, what it answers and its example body; deprecated routes say so. No third-party script (the CSP allows none).
'use strict';

function node(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) if (k === 'text') n.textContent = v; else n.setAttribute(k, v);
    n.append(...kids.filter((k) => k !== null && k !== undefined));
    return n;
}

function apiSections(doc) {
    const byTag = new Map();
    for (const [path, methods] of Object.entries(doc.paths || {})) {
        for (const [method, op] of Object.entries(methods)) {
            const tag = (op.tags || ['Other'])[0];
            if (!byTag.has(tag)) byTag.set(tag, []);
            byTag.get(tag).push({ path, method: method.toUpperCase(), op });
        }
    }
    return [...byTag.entries()].map(([tag, ops]) => ({ tag, ops }));
}

function drawApi(doc) {
    const box = document.getElementById('api-docs');
    box.replaceChildren(...apiSections(doc).map(({ tag, ops }) => node('section', { class: 'card setting-card' },
        node('h2', { text: tag }),
        ...ops.map(({ path, method, op }) => node('details', { class: 'entry', 'data-route': path },
            node('summary', { class: 'entry__head' }, node('code', { text: `${method} ${path}` }),
                op.deprecated ? node('span', { class: 'entry__kind', text: ' deprecated' }) : null,
                node('span', { class: 'entry__kind', text: op['x-who'] ? ` · ${op['x-who']}` : '' })),
            node('p', { class: 'setting__help', text: op.description || '' }),
            op.requestBody?.content?.['application/json']?.example !== undefined
                ? node('pre', {}, node('code', { text: JSON.stringify(op.requestBody.content['application/json'].example, null, 2) })) : null)))));
}

if (typeof document === 'object' && document.addEventListener) {
    document.addEventListener('DOMContentLoaded', async () => {
        try {
            const res = await fetch('/api/openapi.json', { cache: 'no-store' });
            if (!res.ok) throw new Error(`the dashboard answered ${res.status}: sign in at the dashboard first`);
            drawApi(await res.json());
        } catch (err) {
            document.getElementById('api-docs').replaceChildren(node('p', { class: 'row__note', role: 'alert', text: err.message }));
        }
    });
}

if (typeof module !== 'undefined') module.exports = { apiSections };
