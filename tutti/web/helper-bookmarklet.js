// The reading helper's bookmarklet (tutti/helper.py): runs in the provider's usage page the owner has open, reads only the
// page's visible text with the parser of helper-parse.js (__PARSE__), and opens the local dashboard with the readings in the
// #fragment. Placeholders are filled per install. Comment lines are dropped and the lines joined with spaces, so every
// statement ends with a semicolon or a brace.
(function () {
    const targets = __TARGETS__;

    // Core parsing logic extracted for testing
    function parseText(text) {
        // Filter out common artifacts like CSS class names that might appear in innerText but aren't real readings
        let cleaned = text.replace(/class:\s*\w+/g, '').replace(/\n\s*\n/g, '\n');
        
        const parsed = (__PARSE__)(cleaned, targets);
        return parsed;
    }

    // Filter out common artifacts like CSS class names that might appear in innerText but aren't real readings
    let text = document.body.innerText.replace(/class:\s*\w+/g, '').replace(/\n\s*\n/g, '\n');
    const parsed = parseText(text);

    if (parsed.loading) {
        alert('This page is still loading (no meter is shown yet), so Tutti saved nothing. Wait until the meters show, then click the button again.');
        return;
    }
    const readings = parsed.readings.map((r) => ({ account: r.account, used: r.used, resets: r.resets, note: 'read from ' + location.host + location.pathname + ' (' + r.how + ')' }));
    if (parsed.missing.length) {
        alert('Tutti could not find ' + parsed.missing.join(', ') + ' on this page (its layout may have changed). The dashboard opens with the form: type the reading there.');
    }
    const first = targets.length ? targets[0].account : '';
    window.open(__ORIGIN__ + '/#reading=' + encodeURIComponent(JSON.stringify({ key: __KEY__, account: first, readings: readings, page: location.host + location.pathname })), 'tutti-dashboard');
})();

// Export the parsing function for unit testing (only if in a test environment or to allow imports)
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { parseText };
}
