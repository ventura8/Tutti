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
    const reason = f.reason ? ' — ' + f.reason : '';
    const until = left > 0 ? 'expires in ' + hygieneAge(left) + ' (tutti yield renew extends it)' : 'expired: lowered at the next self-heal round';
    return `Yield flag: ${who}${reason} · up ${hygieneAge(now - f.at)} · ${until}`;
}

const HYGIENE_DISK = {
    stop: { tone: 'bad', why: ': nothing new is sent to it until there is 8 GB free' },
    warn: { tone: 'warn', why: ': under 15 GB, the clean-up runs and the biggest items are below' },
    ok: { tone: undefined, why: '' },
};

function hygieneDisk(d) {
    if (d?.free_gb === null || d?.free_gb === undefined) return null;
    const look = HYGIENE_DISK[d.level] || HYGIENE_DISK.ok;
    const all = Object.entries(d.all || {}).map(([k, v]) => `${k} ${tuttiNum(v)} GB`).join(' · ');
    return el('p', { class: 'row__note', 'data-tone': look.tone, text: `Free space: ${all}${look.why}` });
}

function hygieneNote(text, tone) {
    return el('p', { class: 'row__note', 'data-tone': tone, text });
}

function hygieneBits(name) {
    const h = (ui.last?.hygiene || {})[name];
    if (!h) return null;
    const lines = [h.flag ? hygieneNote(hygieneFlag(h.flag)) : null, hygieneDisk(h.disk),
        ...(h.biggest || []).map((item) => hygieneNote(`· ${item}`))];
    if (h.wsl) lines.push(hygieneNote(h.wsl, /not restarted|did not/.test(h.wsl) ? 'warn' : undefined));
    if (h.note) lines.push(hygieneNote(h.note));
    if (h.reclaimed_week_gb > 0) lines.push(hygieneNote(`Reclaimed ${tuttiNum(h.reclaimed_week_gb)} GB this week`));
    const shown = lines.filter(Boolean);
    return shown.length ? el('div', { class: 'hygiene' }, ...shown) : null;
}
