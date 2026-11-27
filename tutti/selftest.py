"""Scheduled browser smoke test and sanitized mock replay cases for the live dashboard."""

import http.client
import json
import os
import threading
import time
from urllib.parse import urlsplit

from . import backlog, config, durable, failover

EVERY = 24 * 3600
STARTUP_DELAY = 60
FILE = 'selftest.json'
HEADER = 'X-Tutti-Self-Test'
VIEWER = {'enabled': True, 'signed_in': True, 'role': 'viewer', 'email': 'self-test@example.com'}
CHECKS = {
    'page': 'The dashboard page did not load. Check the dashboard HTTP response.',
    'viewer': 'The dashboard did not render the viewer role. Check the read-only sign-in path.',
    'state': 'The dashboard did not render live state. Check the state API and dashboard rendering.',
    'script': 'The dashboard raised a browser error. Check dashboard script initialization.',
}
SELECTORS = {'viewer': 'body[data-role="viewer"]', 'state': '#live[data-state="ok"]'}
UNAVAILABLE = 'Chromium could not start. Install Playwright and Chromium for the dashboard service (see docs/TESTING.md).'


def browser_route(route, base, permit):
    """Allow only same-origin GETs; never send the permit through redirects."""
    request = route.request
    parsed = urlsplit(request.url)
    if request.method != 'GET' or f'{parsed.scheme}://{parsed.netloc}' != base:
        return route.abort()
    return fetch_live(route, parsed, permit)


def fetch_live(route, url, permit):
    """Direct HTTP to the local server. The permit never goes through a proxy or redirect."""
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
    try:
        path = f'{url.path}?{url.query}' if url.query else url.path
        conn.request('GET', path, headers={HEADER: permit})
        response = conn.getresponse()
        if 300 <= response.status < 400:
            return route.fulfill(status=502, body='')
        headers = {k: v for k, v in response.getheaders() if k.lower() != 'transfer-encoding'}
        return route.fulfill(status=response.status, headers=headers, body=response.read())
    except (OSError, http.client.HTTPException):
        return route.abort()
    finally:
        conn.close()


def visible(page, selector, timeout, timeout_error):
    try:
        page.locator(selector).wait_for(state='attached', timeout=timeout)
    except timeout_error:
        return False
    return True


def probe(page, base, timeout=10000):
    """Run the same browser assertions in the live and signed-in mock dashboards."""
    from playwright.sync_api import Error, TimeoutError
    errors = []
    page.on('pageerror', lambda _: errors.append(True))
    try:
        response = page.goto(f'{base}/#dashboard', wait_until='load', timeout=timeout)
    except Error:
        return {'page': False}
    if response is None or not response.ok:
        return {'page': False}
    results = {'page': True}
    for check, selector in SELECTORS.items():
        results[check] = visible(page, selector, timeout, TimeoutError)
    return {**results, 'script': not errors}


def run_browser(base, permit):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(timeout=15000)
        try:
            ctx = browser.new_context(service_workers='block', accept_downloads=False)
            return probe(live_page(ctx, base, permit), base)
        finally:
            browser.close()


def live_page(context, base, permit):
    context.route('**/*', lambda route: browser_route(route, base, permit))
    context.route_web_socket('**/*', lambda socket: socket.close())
    return context.new_page()


def fixture_for(check):
    """A declarative mock browser case. Only known check IDs can enter a fixture."""
    return {'check': check, 'expected': False, 'finding': CHECKS[check]}


def record_failures(cfg, checks):
    failures = [fixture_for(check) for check in CHECKS if checks.get(check) is False]
    if not failures or not failover.may_write(cfg):
        return
    folder = os.path.join(config.state_dir(cfg), 'selftest-fixtures')
    os.makedirs(folder, exist_ok=True)
    with backlog.locked(cfg) as data:
        for fixture in failures:
            check = fixture['check']
            durable.dump_json(os.path.join(folder, f'{check}.json'), fixture, indent=1)
            backlog.add(data, backlog.new_task(
                'tutti', f'Fix live self-test failure: {check}', source='selftest',
                check='Replay the fixture in the mock browser suite, then verify the live self-test passes',
                notes=[fixture['finding'], json.dumps(fixture), 'Keep the sanitized regression case in tests/fixtures/selftest/']))


def result_for(checks, now):
    """Compare observed live checks with the passing mock browser contract."""
    return {'status': 'fail' if False in checks.values() else 'ok', 'timestamp': now, 'checks': checks,
            'missed': [CHECKS[check] for check in CHECKS if checks.get(check) is False]}


def load(cfg):
    try:
        with open(os.path.join(config.state_dir(cfg), FILE), encoding='utf8') as f:
            result = json.load(f)
        return result if isinstance(result, dict) and 'started' in result else {}
    except (OSError, ValueError):
        return {}


def due(last, started, now):
    if now < started + STARTUP_DELAY:
        return False
    timestamp = last.get('timestamp')
    return last.get('started') != started or not isinstance(timestamp, (int, float)) or now - timestamp >= EVERY


class Runner:
    """One background browser per dashboard, checked on the active collector's schedule."""

    def __init__(self, started):
        self.started = started
        self.running = threading.Lock()

    def tick(self, cfg, base, permit):
        if not due(load(cfg), self.started, time.time()) or not self.running.acquire(blocking=False):
            return
        threading.Thread(target=self.work, args=(cfg, base, permit), daemon=True).start()

    def work(self, cfg, base, permit):
        try:
            result = self.measure(cfg, base, permit)
            if failover.may_write(cfg):
                durable.dump_json(os.path.join(config.state_dir(cfg), FILE), {**result, 'started': self.started}, indent=1)
        finally:
            self.running.release()

    @staticmethod
    def measure(cfg, base, permit):
        try:
            checks = run_browser(base, permit)
        except Exception:
            # Browser errors may contain live request URLs or values. Persist only the fixed setup message.
            return {'status': 'unavailable', 'timestamp': time.time(), 'message': UNAVAILABLE}
        record_failures(cfg, checks)
        return result_for(checks, time.time())
