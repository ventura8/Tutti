"""The live self-test's browser (`tutti selftest live`, tutti/selftest.py): tests/e2e/checks.js and axe on every tab of the
running dashboard, signed in as the read-only self-test viewer, at phone, tablet, laptop and 4K, light and dark.

Read-only twice over: the identity is a viewer (the server refuses its changes), and every request that is not a GET is
aborted here before it leaves the browser. Only navigation, the Me/Household switch (kept in the browser) and disclosures.

Usage: TUTTI_LIVE_SESSION=<id> TUTTI_LIVE_COOKIE=<name> python3 tests/e2e_live/live.py <base> <out folder> [--widths phone,laptop]
Writes <out>/result.json {checked, findings: [{view, width, theme, check, detail}], state} and a screenshot per finding."""
import json
import os
import re
import sys
from urllib.parse import urlsplit

import axe_playwright_python
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
E2E = os.path.join(os.path.dirname(HERE), 'e2e')
CHECKS = open(os.path.join(E2E, 'checks.js'), encoding='utf8').read()
AXE = open(os.path.join(os.path.dirname(axe_playwright_python.__file__), 'axe.min.js'), encoding='utf8').read()
AXE_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice']
WIDTHS = {'phone': (390, 844), 'tablet': (820, 1180), 'laptop': (1440, 900), '4k': (3840, 2160)}
THEMES = ('light', 'dark')
LAYOUT = '[...tuttiChecks.layout(), ...tuttiChecks.headlinesFit()]'
READY = "document.getElementById('live-text') && document.getElementById('live-text').textContent !== 'Loading'"


def guard(route):
    """Only GETs leave the browser: a page that would POST (a click, a heartbeat) changes nothing on the live system."""
    if route.request.method == 'GET':
        route.continue_()
    else:
        route.abort()


def axe(page):
    if not page.evaluate('typeof axe !== "undefined"'):
        page.evaluate(AXE)
    res = page.evaluate('(t) => axe.run(document, { runOnly: { type: "tag", values: t }, resultTypes: ["violations"] })', AXE_TAGS)
    return [f'{v["id"]}: {v["help"]} at {", ".join(" ".join(n["target"]) for n in v["nodes"][:3])}' for v in res['violations']]


def views(page):
    names = page.evaluate("""[...document.querySelectorAll('nav.tabs a.tabs__tab')].filter((a) => a.offsetParent !== null)
        .map((a) => [a.textContent.trim(), new URL(a.href).hash])""")
    out = [(n, (lambda h=h: page.evaluate('(h) => { location.hash = h; }', h))) for n, h in names]

    def household():
        page.evaluate("location.hash = '#dashboard'")
        page.wait_for_timeout(300)
        page.click('[data-view-pick="household"]')
    return [*out, ('Household', household)]


def main(base, out, widths):
    sid, cookie = os.environ.get('TUTTI_LIVE_SESSION'), os.environ.get('TUTTI_LIVE_COOKIE')
    if not sid or not cookie:
        sys.exit('TUTTI_LIVE_SESSION and TUTTI_LIVE_COOKIE are unset: run it through `tutti selftest live`')
    host = urlsplit(base).hostname
    findings, checked, state = [], 0, None
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for width in widths:
            for theme in THEMES:
                w, h = WIDTHS[width]
                ctx = browser.new_context(viewport={'width': w, 'height': h}, color_scheme=theme, service_workers='block')
                ctx.add_cookies([{'name': cookie, 'value': sid, 'domain': host, 'path': '/', 'httpOnly': True, 'sameSite': 'Strict'}])
                ctx.route('**/*', guard)
                page = ctx.new_page()
                console = []
                page.on('console', lambda m, c=console: c.append(m.text) if m.type == 'error' else None)
                page.on('pageerror', lambda e, c=console: c.append(str(e)))
                page.goto(f'{base}/#dashboard')
                page.wait_for_function(READY, timeout=30000)
                page.wait_for_timeout(1500)
                page.evaluate(CHECKS)
                if state is None:
                    r = page.request.get(f'{base}/api/state')
                    state = r.json() if r.ok else None
                for name, open_it in views(page):
                    open_it()
                    page.wait_for_timeout(700)
                    checked += 1
                    found = [('layout', d) for d in page.evaluate(LAYOUT)]
                    found += [('copy', d) for d in page.evaluate('tuttiChecks.copyLint()')]
                    if width in ('phone', 'laptop'):
                        found += [('axe', d) for d in axe(page)]
                    for kind, detail in found:
                        findings.append({'view': name, 'width': width, 'theme': theme, 'check': kind, 'detail': detail})
                    if found:
                        shot = re.sub(r'\W+', '-', f'{name}-{width}-{theme}').lower() + '.png'
                        page.screenshot(path=os.path.join(out, shot), full_page=True)
                for c in sorted(set(console)):
                    findings.append({'view': 'page', 'width': width, 'theme': theme, 'check': 'console', 'detail': c})
                ctx.close()
        browser.close()
    with open(os.path.join(out, 'result.json'), 'w', encoding='utf8') as f:
        json.dump({'checked': checked, 'findings': findings, 'state': state}, f)


if __name__ == '__main__':
    args = sys.argv[1:]
    pick = list(WIDTHS)
    if '--widths' in args:
        i = args.index('--widths')
        pick = args[i + 1].split(',')
        del args[i:i + 2]
    main(args[0].rstrip('/'), args[1], pick)
