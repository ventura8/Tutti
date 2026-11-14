// The reading helper's parser (tutti/helper.py): one function expression, (text, targets, now) => {readings, missing}.
// The bookmarklet runs it on the page's visible text; `tutti usage page-script` wraps it for an orchestrator's browser tool;
// the tests run it in node on a copy of the real page's text. Comment lines are dropped and the lines joined with spaces,
// so every statement ends with a semicolon or a brace.
// targets is [{account, label}]. A meter is read line by line (Cursor's spending page, 2026-11-14: "Cursor Models · Includes
// ...", "25% used", ..., "Other Models", "14% used", then "Grok Bot", "Weekly usage", "0% used", "Resets Nov 15"): it starts
// at the first line that begins with the label (else the first that contains it) and ends at the line with its amount ("N%
// used", "$spent of $limit", else a bare "N%"), plus the next line when that line says "Resets <date>". A reset in the meter
// wins; otherwise the page's "limits reset <date>", else the end of a "billing period <a> - <b>". A "Resets" line of another
// meter (Grok's weekly) is never taken for this one.
function (text, targets, now) {
    const DATE = String.raw`([A-Z][a-z]{2,8}\.?\s+\d{1,2}(?:,?\s+\d{4})?)`;
    const clean = String(text || '').replaceAll(' ', ' ');
    const lines = clean.split('\n').map((l) => l.trim()).filter((l) => l);
    const today = now || Date.now();
    function meterStart(label) {
        if (!label) {
            return 0;
        }
        const low = label.toLowerCase();
        const head = lines.findIndex((l) => l.toLowerCase().startsWith(low));
        return head >= 0 ? head : lines.findIndex((l) => l.toLowerCase().includes(low));
    }
    function amount(line) {
        const pct = /(\d{1,3}(?:\.\d+)?)\s*%[^%]{0,50}?\b(?:used|spent|consumed)/i.exec(line) || /\b(?:used|spent)[^%]{0,40}?(\d{1,3}(?:\.\d+)?)\s*%/i.exec(line);
        if (pct) {
            return { used: Number.parseFloat(pct[1]), how: 'percent' };
        }
        const usd = /\$\s?([\d,]+(?:\.\d+)?)\s*(?:of|\/|out of)\s*\$\s?([\d,]+(?:\.\d+)?)/i.exec(line);
        if (usd) {
            const spent = Number.parseFloat(usd[1].replaceAll(',', ''));
            const limit = Number.parseFloat(usd[2].replaceAll(',', ''));
            return limit > 0 ? { used: Math.round((1000 * spent) / limit) / 10, how: '$' + spent + ' of $' + limit } : null;
        }
        return null;
    }
    function bare(line) {
        const m = /(\d{1,3}(?:\.\d+)?)\s*%/.exec(line);
        return m ? { used: Number.parseFloat(m[1]), how: 'first percent' } : null;
    }
    function meter(label) {
        const start = meterStart(label);
        if (start < 0) {
            return null;
        }
        const stop = Math.min(lines.length, start + 8);
        for (const find of [amount, bare]) {
            for (let i = start; i < stop; i++) {
                const found = find(lines[i]);
                if (found) {
                    const next = lines[i + 1] || '';
                    return { found: found, part: lines.slice(start, i + 1).join('\n') + (/^resets?\b/i.test(next) ? '\n' + next : '') };
                }
            }
        }
        return null;
    }
    function dateOf(m) {
        if (!m) {
            return null;
        }
        const year = new Date(today).getFullYear();
        const withYear = (y) => Date.parse(/\d{4}/.test(m[1]) ? m[1] : m[1] + ' ' + y);
        let t = withYear(year);
        if (Number.isNaN(t)) {
            return null;
        }
        if (t < today - 864e5) {
            t = withYear(year + 1);
        }
        return new Date(t).toISOString();
    }
    function resetDate(part) {
        const resets = new RegExp(String.raw`[Rr]esets?\s+(?:on\s+)?` + DATE);
        const limits = new RegExp(String.raw`limits?\s+resets?\s+(?:on\s+)?` + DATE, 'i');
        const period = new RegExp(String.raw`(?:billing period|billing cycle)[^\n]{0,40}?[-–]\s*` + DATE, 'i');
        return dateOf(resets.exec(part)) || dateOf(limits.exec(clean)) || dateOf(period.exec(clean));
    }
    const readings = [];
    const missing = [];
    for (const t of targets) {
        const m = meter(t.label);
        if (m) {
            readings.push({ account: t.account, used: m.found.used, resets: resetDate(m.part), how: t.label || m.found.how });
        } else {
            missing.push(t.label || t.account);
        }
    }
    return { readings: readings, missing: missing };
}
