// Machine hygiene on each machine card (tutti/hygiene.py, docs/SELFHEAL.md): who holds its yield flag and for how long, its
// disks (a warning under 15 GB with the biggest reclaimable items), a hung WSL restarted, and the week's clean-up.
/* exported hygieneBits */

function hygieneAge(seconds) {
    const s = Math.max(0, Math.round(seconds));
    return s < 3600 ? `${Math.floor(s / 60)} min` : `${Math.floor(s / 3600)} h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')} min`;
}

function hygieneFlag(f) {
    const now = Date.now() / 1000;
    const left = f.expires - now;
    const who = f.legacy ? 'an older flag (owner not recorded)' : (f.owner || 'owner unknown');
    return `Yield flag: ${who}${f.reason ? ` — ${f.reason}` : ''} · up ${hygieneAge(now - f.at)} · `
        + (left > 0 ? `expires in ${hygieneAge(left)} (tutti yield renew extends it)` : 'expired: lowered at the next self-heal round');
}

function hygieneBits(name) {
    const h = (ui.last?.hygiene || {})[name];
    if (!h) return null;
    const lines = [];
    if (h.flag) lines.push(el('p', { class: 'row__note', text: hygieneFlag(h.flag) }));
    const d = h.disk;
    if (d && d.free_gb !== null && d.free_gb !== undefined) {
        const tone = d.level === 'stop' ? 'bad' : d.level === 'warn' ? 'warn' : undefined;
        const all = Object.entries(d.all || {}).map(([k, v]) => `${k} ${tuttiNum(v)} GB`).join(' · ');
        const why = d.level === 'stop' ? ': nothing new is sent to it until there is 8 GB free'
            : d.level === 'warn' ? ': under 15 GB, the clean-up runs and the biggest items are below' : '';
        lines.push(el('p', { class: 'row__note', 'data-tone': tone, text: `Free space: ${all}${why}` }));
    }
    for (const item of h.biggest || []) lines.push(el('p', { class: 'row__note', text: `· ${item}` }));
    if (h.wsl) lines.push(el('p', { class: 'row__note', 'data-tone': /not restarted|did not/.test(h.wsl) ? 'warn' : undefined, text: h.wsl }));
    if (h.note) lines.push(el('p', { class: 'row__note', text: h.note }));
    if (h.reclaimed_week_gb > 0) lines.push(el('p', { class: 'row__note', text: `Reclaimed ${tuttiNum(h.reclaimed_week_gb)} GB this week` }));
    return lines.length ? el('div', { class: 'hygiene' }, ...lines) : null;
}
