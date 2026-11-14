// Tutti dashboard: the "Private network (optional)" card (docs/NETWORK.md). Uses app.js's helpers (el, $, showBanner, TOKEN).
// Low-key on purpose: Tutti needs no VPN. It lists the person's steps `tutti vpn setup` left, shows a phone's WireGuard QR code
// once, and opens WireGuard's UDP port on the router only after an explicit click and a confirm.
'use strict';

async function vpnPost(body) {
    const res = await fetch('/api/vpn', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': TOKEN }, body: JSON.stringify(body),
    });
    const answer = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}`);
    return answer;
}

function vpnButton(text, onClick) {
    const b = el('button', { class: 'btn btn--tonal', type: 'button', text });
    b.addEventListener('click', () => { void onClick(b); });
    return b;
}

async function vpnQr(phone) {
    const a = await vpnPost({ action: 'qr', phone });
    const box = $('vpn-qr');
    const img = a.svg ? el('img', { alt: `WireGuard QR code for ${phone}`, width: '240', height: '240' }) : null;
    if (img) img.src = `data:image/svg+xml;base64,${btoa(a.svg)}`;
    box.replaceChildren(el('p', { class: 'row__note', text: a.note }), img || el('pre', { text: a.text }),
        vpnButton('Hide', async () => { box.replaceChildren(); }));
}

async function vpnRouter(button) {
    const r = await vpnPost({ action: 'router' });
    if (!r.kind) { showBanner(el('strong', { text: 'Router: ' }), r.steps); return; }
    if (!window.confirm(`Open UDP ${r.port} on your router (${r.kind}) to this computer? Only WireGuard's port, nothing else.`)) return;
    button.disabled = true;
    const a = await vpnPost({ action: 'open-port', consent: true });
    showBanner(el('strong', { text: a.ok ? 'Router: port open. ' : 'Router: not opened. ' }), a.text || a.steps || '');
}

function vpnStep(s) {
    const extra = [];
    if (s.id.startsWith('qr-')) extra.push(vpnButton('Show QR', () => vpnQr(s.id.slice(3))));
    if (s.id === 'router') extra.push(vpnButton('Open the port', vpnRouter));
    return el('li', { class: 'row' }, el('div', { class: 'row__name', text: s.title }),
        s.command ? el('p', { class: 'row__note' }, el('code', { text: s.command })) : '',
        s.url ? el('p', { class: 'row__note', text: s.url }) : '',
        s.detail ? el('p', { class: 'row__note', text: s.detail }) : '',
        extra.length ? el('div', { class: 'row__stats' }, ...extra) : '');
}

async function renderVpn() {
    try {
        const a = await vpnPost({ action: 'status' });
        if (a.provider) $('vpn-intro').textContent = `${a.provider}: what is left for you, in order (\`tutti vpn setup\` again continues).`;
        $('vpn-steps').replaceChildren(...a.checklist.map(vpnStep));
    } catch (err) {
        $('vpn-meta').textContent = `Already have a VPN to your home? Nothing to do. (The setup state could not be read: ${err.message})`;
    }
}

document.addEventListener('DOMContentLoaded', () => { void renderVpn(); });
