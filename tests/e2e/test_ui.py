"""The dashboard in a real browser (Playwright's Chromium, pinned in requirements-dev.txt): every tab, for an admin and a viewer
signed in through a fake Google, at phone, tablet, laptop and 4K widths, light and dark, at 200 % zoom and with WCAG 1.4.12 text
spacing. Layout (no overlap, no clipped or truncated text, no sideways scroll, everything inside the viewport), accessibility
(axe-core with WCAG 2.2 A/AA and best practices, the keyboard, target size, contrast, landmarks and headings, labels, live
regions, reduced motion, icon names, form errors) and roles. docs/TESTING.md, *Browser tests*.

Run: PYTHONPATH=tests python3 -m unittest discover -s tests/e2e   (scripts/check.sh runs it; TUTTI_UI_SHOTS=<folder> keeps screenshots)"""
import os
import re
import unittest

try:
    import axe_playwright_python
    from playwright.sync_api import sync_playwright
except ImportError as exc:
    raise unittest.SkipTest(f'browser e2e dependency missing: {exc}') from exc

try:
    from harness import Dashboard
except ImportError:
    from tests.e2e.harness import Dashboard

HERE = os.path.dirname(os.path.abspath(__file__))


def read(path):
    with open(path, encoding='utf8') as f:
        return f.read()


CHECKS = read(os.path.join(HERE, 'checks.js'))
AXE = read(os.path.join(os.path.dirname(axe_playwright_python.__file__), 'axe.min.js'))   # axe-core 4.12, pinned with the package
AXE_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice']
WIDTHS = {'phone': (360, 780), 'tablet': (768, 1024), 'laptop': (1440, 900), '4k': (3840, 2160)}
THEMES = ('light', 'dark')
SHOTS = os.environ.get('TUTTI_UI_SHOTS')
ADMIN, VIEWER = 'owner', 'guest'
LABEL = {ADMIN: 'admin', VIEWER: 'viewer'}


class World:
    """One dashboard and one browser for the whole module; a signed-in storage state per role."""

    dash = pw = browser = None
    states = {}


def setUpModule():
    World.dash = Dashboard()
    World.pw = sync_playwright().start()
    World.browser = World.pw.chromium.launch()
    for sub in (ADMIN, VIEWER):
        ctx = new_context(None)
        answer = World.dash.sign_in(ctx, sub)
        if sub == ADMIN:
            assert answer['role'] == 'admin', answer   # the first user is the admin
            admin_ctx = ctx
        else:
            assert answer['role'] == 'pending', answer
            World.dash.admin_post(admin_ctx, '/api/auth/users', {'action': 'approve', 'sub': VIEWER})
        World.states[sub] = ctx.storage_state()
    for ctx in World.browser.contexts:
        ctx.close()


def tearDownModule():
    World.browser.close()
    World.pw.stop()
    World.dash.close()


def new_context(role, width='laptop', theme='light', zoom=1, motion='no-preference'):
    w, h = WIDTHS[width]
    ctx = World.browser.new_context(viewport={'width': int(w / zoom), 'height': int(h / zoom)}, device_scale_factor=zoom,
                                    color_scheme=theme, reduced_motion=motion, bypass_csp=True, locale='en-US', timezone_id='UTC',
                                    storage_state=World.states.get(role), service_workers='block')
    base = World.dash.base
    ctx.route('**/*', lambda r: r.continue_() if r.request.url.startswith(base) else r.abort())   # no network: Google is fake
    return ctx


def open_view(ctx, hash_=''):
    page = ctx.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(f'{World.dash.base}/{hash_}')
    page.wait_for_function("document.getElementById('live-text') && document.getElementById('live-text').textContent !== 'Loading'")
    page.wait_for_function("document.querySelectorAll('#usage > *').length > 0")
    page.wait_for_timeout(300)   # the cards after the first state are drawn in the same task; the role's own fetches settle
    page.evaluate(CHECKS)
    page.errors = errors
    return page


def tabs(page):
    """The views the signed-in user sees in the tab bar: [(name, hash)]."""
    return page.evaluate("""[...document.querySelectorAll('nav.tabs a.tabs__tab')].filter((a) => a.offsetParent !== null)
        .map((a) => [a.textContent.trim(), new URL(a.href).hash])""")


def go(page, hash_):
    page.evaluate('(h) => { location.hash = h; }', hash_ or '#')
    page.wait_for_timeout(400)   # a view's own fetch (Settings, Users) answers from the local server well within this


def shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, f'{name}.png'), full_page=True)


def axe(page):
    if not page.evaluate('typeof axe !== "undefined"'):
        page.evaluate(AXE)
    res = page.evaluate('(tags) => axe.run(document, { runOnly: { type: "tag", values: tags }, resultTypes: ["violations"] })', AXE_TAGS)
    return [f'{v["id"]} ({v["impact"]}): {v["help"]} at {", ".join(" ".join(n["target"]) for n in v["nodes"][:4])}'
            for v in res['violations']]


class Layout(unittest.TestCase):
    maxDiff = None
    """No overlap, no clipped or truncated text, no sideways scroll, nothing past the viewport: every tab, width and theme."""


    def run_matrix(self, role):
        problems = {}
        for width in WIDTHS:
            for theme in THEMES:
                ctx = new_context(role, width, theme)
                page = open_view(ctx)
                for name, hash_ in tabs(page):
                    go(page, hash_)
                    found = page.evaluate('tuttiChecks.layout()')
                    if found:
                        problems[f'{role} {name} {width} {theme}'] = found
                    shot(page, f'{LABEL[role]}-{name.lower()}-{width}-{theme}')
                self.assertEqual(page.errors, [], f'script errors on {role} {width} {theme}')
                ctx.close()
        self.assertEqual(problems, {})

    def test_admin_every_tab_width_and_theme(self):
        self.run_matrix(ADMIN)

    def test_viewer_every_tab_width_and_theme(self):
        self.run_matrix(VIEWER)

    def test_200_percent_zoom(self):
        problems = {}
        for role in (ADMIN, VIEWER):
            ctx = new_context(role, 'laptop', 'light', zoom=2)
            page = open_view(ctx)
            for name, hash_ in tabs(page):
                go(page, hash_)
                found = page.evaluate('tuttiChecks.layout()')
                if found:
                    problems[f'{role} {name} 200%'] = found
                shot(page, f'{LABEL[role]}-{name.lower()}-zoom200')
            ctx.close()
        self.assertEqual(problems, {})

    def test_wcag_text_spacing(self):
        problems = {}
        for width in ('phone', 'laptop'):
            ctx = new_context(ADMIN, width)
            page = open_view(ctx)
            page.evaluate('tuttiChecks.textSpacing()')
            for name, hash_ in tabs(page):
                go(page, hash_)
                found = page.evaluate('tuttiChecks.layout()')
                if found:
                    problems[f'{name} {width} text spacing'] = found
                shot(page, f'admin-{name.lower()}-{width}-text-spacing')
            ctx.close()
        self.assertEqual(problems, {})

    def test_the_sign_in_page(self):
        problems = {}
        for width in WIDTHS:
            for theme in THEMES:
                ctx = new_context(None, width, theme)
                page = ctx.new_page()
                page.goto(f'{World.dash.base}/login')
                page.evaluate(CHECKS)
                found = page.evaluate('tuttiChecks.layout()') + axe(page)
                if found:
                    problems[f'login {width} {theme}'] = found
                shot(page, f'login-{width}-{theme}')
                ctx.close()
        self.assertEqual(problems, {})


class Accessibility(unittest.TestCase):
    maxDiff = None

    def test_axe_wcag22_and_best_practices_every_tab_both_themes(self):
        problems = {}
        for role in (ADMIN, VIEWER):
            for width in ('phone', 'laptop'):
                for theme in THEMES:
                    ctx = new_context(role, width, theme)
                    page = open_view(ctx)
                    for name, hash_ in tabs(page):
                        go(page, hash_)
                        found = axe(page)
                        if found:
                            problems[f'{role} {name} {width} {theme}'] = found
                    ctx.close()
        self.assertEqual(problems, {})

    def test_targets_icon_names_landmarks_headings_and_live_regions(self):
        problems = {}
        for role in (ADMIN, VIEWER):
            for width in ('phone', 'laptop'):
                ctx = new_context(role, width)
                page = open_view(ctx)
                for name, hash_ in tabs(page):
                    go(page, hash_)
                    found = page.evaluate('[...tuttiChecks.targetSize(), ...tuttiChecks.iconNames(), ...tuttiChecks.landmarks(), '
                                          '...tuttiChecks.liveRegions()]')
                    if found:
                        problems[f'{role} {name} {width}'] = found
                ctx.close()
        self.assertEqual(problems, {})

    def test_the_status_line_is_a_polite_live_region(self):
        ctx = new_context(ADMIN)
        page = open_view(ctx)
        self.assertEqual(page.get_attribute('#live', 'aria-live'), 'polite')
        page.click('#refresh')
        page.wait_for_function("document.getElementById('live-text').textContent.length > 0")
        self.assertTrue(page.text_content('#live-text').strip())
        ctx.close()

    def test_keyboard_reaches_every_control_shows_focus_and_never_traps(self):
        for role in (ADMIN, VIEWER):
            ctx = new_context(role)
            page = open_view(ctx)
            for name, hash_ in tabs(page):
                go(page, hash_)
                with self.subTest(role=role, tab=name):
                    want = page.evaluate('tuttiChecks.tabbables()')
                    page.evaluate('tuttiChecks.startAtTop()')   # the next Tab goes to the page's first control
                    reached, unshown = [], []
                    for _ in range(2 * len(want) + 10):
                        page.keyboard.press('Tab')
                        at = page.evaluate('tuttiChecks.active()')
                        if at == 'body' or (reached and at == reached[0]):
                            break   # wrapped round to the start: no trap
                        if not page.evaluate('tuttiChecks.focusShown()'):
                            unshown.append(at)
                        reached.append(at)
                    else:
                        self.fail(f'focus never came back round after {len(reached)} Tabs: a keyboard trap near {reached[-3:]}')
                    self.assertEqual([w for w in want if w not in reached], [], 'controls the Tab key never reaches')
                    self.assertEqual(unshown, [], 'focused with no visible focus indicator')
            ctx.close()

    def test_escape_closes_an_open_disclosure_and_returns_focus(self):
        ctx = new_context(ADMIN)
        page = open_view(ctx, '#dashboard')
        summaries = page.locator('details > summary:visible')
        self.assertGreater(summaries.count(), 0)
        for i in range(min(summaries.count(), 6)):
            s = summaries.nth(i)
            s.focus()
            page.keyboard.press('Enter')
            self.assertTrue(s.evaluate('(x) => x.parentElement.open'))
            page.keyboard.press('Escape')
            self.assertFalse(s.evaluate('(x) => x.parentElement.open'), f'Esc left disclosure {i} open')
            self.assertTrue(s.evaluate('(x) => document.activeElement === x'), 'focus did not return to the summary')
        ctx.close()

    def test_reduced_motion_is_respected(self):
        ctx = new_context(ADMIN, motion='reduce')
        page = open_view(ctx)
        for name, hash_ in tabs(page):
            go(page, hash_)
            self.assertEqual(page.evaluate('tuttiChecks.reducedMotion()'), [], name)
        ctx.close()

    def test_a_form_error_is_announced_and_tied_to_its_field(self):
        ctx = new_context(ADMIN)
        page = open_view(ctx, '#dashboard')
        page.fill('#autocompact-pct', '5')
        page.click('#autocompact-form button[type=submit]')
        field = page.locator('#autocompact-pct')
        self.assertEqual(field.get_attribute('aria-invalid'), 'true')
        ids = (field.get_attribute('aria-describedby') or '').split() + (field.get_attribute('aria-errormessage') or '').split()
        errors = [page.locator(f'#{i}') for i in ids if page.locator(f'#{i}[role=alert]').count()]
        self.assertTrue(errors, 'no role=alert error message is tied to the field')
        self.assertRegex(errors[0].text_content(), r'10')
        page.fill('#autocompact-pct', '50')
        self.assertNotEqual(field.get_attribute('aria-invalid'), 'true')   # corrected: the error clears as the owner types
        ctx.close()

    def test_a_good_form_value_raises_no_error(self):
        ctx = new_context(ADMIN)
        page = open_view(ctx, '#dashboard')
        page.fill('#autocompact-pct', '60')
        self.assertNotEqual(page.get_attribute('#autocompact-pct', 'aria-invalid'), 'true')
        ctx.close()


ADMIN_CONTROLS = ('#refresh-readings', '#autopilot-toggle', '#selfimprove-toggle', '#telemetry-toggle', '#users-tab', '#update-now',
                  '#connect-all', '#main-form', '#autocompact-form')


class Roles(unittest.TestCase):
    maxDiff = None

    def test_a_viewer_sees_no_admin_control(self):
        ctx = new_context(VIEWER)
        page = open_view(ctx, '#dashboard')
        self.assertEqual([n for n, _ in tabs(page)], ['Chat', 'Dashboard'])
        shown = page.evaluate("""[...document.querySelectorAll('[data-admin], button, input, select, textarea, form, a.link')]
            .filter((e) => e.offsetParent !== null && !e.closest('.view-ok, .seg, #job-filter, nav.tabs'))
            .filter((e) => !['refresh', 'signout', 'job-project', 'install'].includes(e.id) && !e.classList.contains('more'))
            .map((e) => e.id || e.className || e.tagName)""")
        self.assertEqual(shown, [])
        for hash_ in ('#settings', '#users'):
            go(page, hash_)
            self.assertFalse(page.is_visible('#settings-view') or page.is_visible('#users-view'), hash_)
        self.assertEqual(page.request.get(f'{World.dash.base}/api/settings').status, 403)
        ctx.close()

    def test_an_admin_sees_every_admin_control(self):
        ctx = new_context(ADMIN)
        page = open_view(ctx, '#dashboard')
        self.assertEqual([n for n, _ in tabs(page)], ['Chat', 'Dashboard', 'Settings', 'Users'])
        hidden = [c for c in ADMIN_CONTROLS if not page.is_visible(c)]
        self.assertEqual(hidden, [])
        go(page, '#settings')
        page.wait_for_selector('#settings-body .setting, #settings-body input, #settings-body select', state='visible')
        go(page, '#users')
        self.assertTrue(page.is_visible('#invite-create'))
        self.assertTrue(re.search(r'guest@example\.com', page.text_content('#users')))
        ctx.close()

    def test_machine_cards_match_the_probe(self):
        """Each card's slot boxes equal its busy, held-back and free slots from /api/state; the text agrees; an empty limit
        says none and stays editable for an admin."""
        ctx = new_context(ADMIN)
        page = open_view(ctx, '#dashboard')
        items = page.evaluate("fetch('/api/state').then((r) => r.json()).then((s) => s.machines.items)")
        cards = page.locator('#machines > li')
        self.assertEqual(cards.count(), len(items))
        for i, m in enumerate(items):
            card = cards.nth(i)
            self.assertEqual(card.locator('.pip').count(), m['slots'])
            if m['free'] is None:   # not reachable: dashed boxes and the slot count only
                self.assertEqual(card.locator(".pip[data-off='true']").count(), m['slots'])
            else:
                self.assertEqual(card.locator(".pip[data-busy='true']").count(), m['busy'])
                self.assertEqual(card.locator(".pip[data-held='true']").count(), m['held_back'])
                self.assertIn(f"{m['free']} of {m['slots']} slot", card.text_content())
            win = card.get_by_label(f"{m['name']}: limited hours, HH-HH")
            self.assertTrue(win.is_editable())
            if not m.get('limit'):
                self.assertEqual((win.input_value(), win.get_attribute('placeholder')), ('', 'none'))
                self.assertIn('Limited hours: none set', card.text_content())
        ctx.close()

    def test_device_cards_match_the_state(self):
        """One card per device: its pill, why and action are /api/state's devices.items[].card, never a second status."""
        ctx = new_context(ADMIN)
        page = open_view(ctx, '#dashboard')
        items = page.evaluate("fetch('/api/state').then((r) => r.json()).then((s) => s.devices.items)")
        for d in items:
            card = page.locator(f"#devices > li[data-device='{d['name']}']")
            self.assertEqual(card.count(), 1)
            if card.get_attribute('data-status') == 'checking':   # not probed yet: no card status to compare
                continue
            self.assertEqual(card.get_attribute('data-status'), d['card']['status'])
            self.assertIn(d['card']['label'], card.locator('.chip').first.text_content())
            if d['card']['why']:
                self.assertIn(d['card']['why'], card.text_content())
            if d['card']['action']:
                self.assertTrue(card.get_by_role('button', name=d['card']['action']['label']).is_visible())
        self.assertNotIn('2.5 °C', page.text_content('#devices'))   # a TV's battery reading is never its temperature
        ctx.close()

    def test_local_model_cards_match_the_state(self):
        """One Local models card per computer, as /api/state's local_models.cards (a dedicated desktop session folds into its
        host's card); the GPU, measured or estimated, the models and the period table fit their card at laptop and phone width."""
        for width in ('laptop', 'phone'):
            ctx = new_context(ADMIN, width)
            page = open_view(ctx, '#dashboard')
            cards = page.evaluate("fetch('/api/state').then((r) => r.json()).then((s) => s.local_models.cards)")
            names = [c['name'] for c in cards]
            self.assertNotIn('gpu-box-desktop', names)
            self.assertEqual(page.locator('#local-models > article').count(), len(cards))
            for c in cards:
                card = page.locator(f"#local-models > article[data-machine='{c['name']}']")
                text = card.text_content()
                self.assertIn(c['gpu'], text)
                self.assertNotIn('GPU not read', text)
                self.assertIn('GPU', text)
                power = card.locator('.account__auto', has_text=' W now').text_content()
                self.assertRegex(power, r'GPU \d+ W ' + ('measured' if c['gpu_measured'] else 'estimated'))
                self.assertEqual(power.startswith('≈'), not c['gpu_measured'])
                for m in c['installed']:
                    self.assertIn(m, text)
                fits = card.evaluate("(a) => [...a.querySelectorAll('.local__periods td, .local__periods th')].every((t) => "
                                     "getComputedStyle(t).whiteSpace === 'nowrap' && t.scrollWidth <= t.clientWidth + 1 "
                                     "&& t.getBoundingClientRect().height < 2 * parseFloat(getComputedStyle(t).fontSize) * 1.6) "
                                     "&& a.scrollWidth <= a.clientWidth + 1")
                self.assertTrue(fits, f"{c['name']} at {width}: a table cell wraps or the card overflows")
            box = {c['name']: c for c in cards}['gpu-box']
            self.assertEqual(box['workers'], ['big', 'big-desktop'])
            self.assertTrue(box['gpu_measured'])
            self.assertIn('qwen3.5:9b', page.locator("#local-models > article[data-machine='gpu-box']").text_content())
            ctx.close()

    def test_numbers_have_no_float_noise(self):
        for role in (ADMIN, VIEWER):
            ctx = new_context(role)
            page = open_view(ctx, '#dashboard')
            text = page.evaluate('document.body.innerText')
            self.assertIsNone(re.search(r'\d+\.\d{4,}', text), re.search(r'.{30}\d+\.\d{4,}.{10}', text))
            self.assertRegex(page.text_content('#usage'), r'\b32\s?%')   # grok-bot's 32.122999999999976 %
            ctx.close()


if __name__ == '__main__':
    unittest.main()
