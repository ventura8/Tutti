// Tutti dashboard: whether the open page should reload for a new Tutti version (pure; app.js and a node test share it).
// 'none': same version; 'reload': update the service worker and reload once; 'stuck': already reloaded once for this
// version and still old (a cached page): never reload again, say so instead. No marker storage: never reload by itself.
function tuttiUpdateDecision(pageVersion, serverVersion, reloadedFor, canMark) {
    if (!pageVersion || !serverVersion || pageVersion === serverVersion) return 'none';
    if (reloadedFor === serverVersion || !canMark) return 'stuck';
    return 'reload';
}
