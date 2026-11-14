// The reading helper's bookmarklet (tutti/helper.py): runs in the provider's usage page the owner has open, reads only the
// page's visible text, and opens the local dashboard with the readings in the #fragment. Placeholders are filled per install.
// __TARGETS__ is [{account, label}]: each account's meter is the text after its label (Cursor's spending page: "Cursor Models"
// ... "25% used", "Other Models" ... "14% used"), cut at the next label; no label means the whole page. In a meter: "N% used",
// else "$spent of $limit", else the first "N%". The reset: "Resets <date>" in the meter, else "limits reset <date>", else the
// end of a "billing period <a> - <b>". Comment lines are dropped and the lines joined with spaces, so every statement ends
// with a semicolon or a brace.
(function () {
    const text = document.body.innerText.replaceAll(' ', ' ');
    const lower = text.toLowerCase();
    const targets = __TARGETS__;
    const DATE = String.raw`([A-Z][a-z]{2,8}\.?\s+\d{1,2}(?:,?\s+\d{4})?)`;
    function section(label) {
        if (!label) {
            return text;
        }
        const start = lower.indexOf(label.toLowerCase());
        if (start < 0) {
            return '';
        }
        let end = Math.min(text.length, start + 400);
        for (const t of targets) {
            const at = t.label ? lower.indexOf(t.label.toLowerCase(), start + label.length) : -1;
            if (at > start && at < end) {
                end = at;
            }
        }
        return text.slice(start, end);
    }
    function percentUsed(part) {
        const m = /(\d{1,3}(?:\.\d+)?)\s*%[^\n%]{0,50}?\b(?:used|spent|consumed)/i.exec(part) || /\b(?:used|spent)[^\n%]{0,40}?(\d{1,3}(?:\.\d+)?)\s*%/i.exec(part);
        return m ? { used: Number.parseFloat(m[1]), how: 'percent' } : null;
    }
    function dollarsUsed(part) {
        const m = /\$\s?([\d,]+(?:\.\d+)?)\s*(?:of|\/|out of)\s*\$\s?([\d,]+(?:\.\d+)?)/i.exec(part);
        if (!m) {
            return null;
        }
        const spent = Number.parseFloat(m[1].replaceAll(',', ''));
        const limit = Number.parseFloat(m[2].replaceAll(',', ''));
        return limit > 0 ? { used: Math.round((1000 * spent) / limit) / 10, how: '$' + spent + ' of $' + limit } : null;
    }
    function anyPercent(part) {
        const m = /(\d{1,3}(?:\.\d+)?)\s*%/.exec(part);
        return m ? { used: Number.parseFloat(m[1]), how: 'first percent' } : null;
    }
    function dateOf(m) {
        if (!m) {
            return null;
        }
        const year = new Date().getFullYear();
        const withYear = (y) => Date.parse(/\d{4}/.test(m[1]) ? m[1] : m[1] + ' ' + y);
        let t = withYear(year);
        if (Number.isNaN(t)) {
            return null;
        }
        if (t < Date.now() - 864e5) {
            t = withYear(year + 1);
        }
        return new Date(t).toISOString();
    }
    function resetDate(part) {
        const resets = new RegExp(String.raw`[Rr]esets?\s+(?:on\s+)?` + DATE);
        const limits = new RegExp(String.raw`limits?\s+resets?\s+(?:on\s+)?` + DATE, 'i');
        const period = new RegExp(String.raw`(?:billing period|billing cycle)[^\n]{0,40}?[-–]\s*` + DATE, 'i');
        return dateOf(resets.exec(part)) || dateOf(limits.exec(text)) || dateOf(period.exec(text)) || dateOf(resets.exec(text));
    }
    const readings = [];
    const missing = [];
    for (const t of targets) {
        const part = section(t.label);
        const found = part ? percentUsed(part) || dollarsUsed(part) || anyPercent(part) : null;
        if (found) {
            readings.push({ account: t.account, used: found.used, resets: resetDate(part), note: 'read from ' + location.host + location.pathname + ' (' + (t.label || found.how) + ')' });
        } else {
            missing.push(t.label || t.account);
        }
    }
    if (missing.length) {
        alert('Tutti could not find ' + missing.join(', ') + ' on this page (its layout may have changed). The dashboard opens with the form: type the reading there.');
    }
    const first = targets.length ? targets[0].account : '';
    window.open(__ORIGIN__ + '/#reading=' + encodeURIComponent(JSON.stringify({ key: __KEY__, account: first, readings: readings })), 'tutti-dashboard');
})();
