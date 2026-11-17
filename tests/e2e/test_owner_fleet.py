"""The dashboard with the owner's own data shapes (serve.py with TUTTI_E2E_FLEET=owner, the 2026-11-16 audit): seven machines,
four accounts at the limit at 97-100 %, eight jobs of which three stalled, long routing reasons and a config edit that does not
load. Every tab and view (Chat, Dashboard, Settings, Users, the Household view, What did I miss?) at phone, tablet, laptop
and 4K, light and dark: the layout checks, axe, headlines that fit, plain copy, no console error, and the error states.

Run: PYTHONPATH=tests python3 -m unittest discover -s tests/e2e   (scripts/check.sh runs it; TUTTI_UI_SHOTS=<folder> keeps screenshots)"""
import unittest

import test_ui as base
from harness import Dashboard
from playwright.sync_api import sync_playwright

SAVED = {}
WIDTHS = {'phone': (390, 844), 'tablet': (820, 1180), 'laptop': (1440, 900), '4k': (3840, 2160)}


def setUpModule():
    SAVED.update(base.WIDTHS)
    base.WIDTHS.update(WIDTHS)   # the owner's own devices: a 390 phone, an 820 tablet
    base.World.dash = Dashboard({'TUTTI_E2E_FLEET': 'owner'})
    base.World.pw = sync_playwright().start()
    base.World.browser = base.World.pw.chromium.launch()
    ctx = base.new_context(None)
    answer = base.World.dash.sign_in(ctx, base.ADMIN)
    assert answer['role'] == 'admin', answer
    base.World.states = {base.ADMIN: ctx.storage_state()}
    ctx.close()


def tearDownModule():
    base.World.browser.close()
    base.World.pw.stop()
    base.World.dash.close()
    base.World.dash = base.World.pw = base.World.browser = None
    base.World.states = {}
    base.WIDTHS.update(SAVED)


def views(page):
    """Every tab, then the Household view and the What did I miss? summary on the Dashboard: [(name, open it)]."""
    def tab(h):
        return lambda: base.go(page, h)

    def household():
        base.go(page, '#dashboard')
        page.click('[data-view-pick="household"]')
        page.wait_for_timeout(300)

    def missed():
        base.go(page, '#dashboard')
        page.click('[data-view-pick="me"]')
        page.click('#welcome-miss')
        page.wait_for_selector('#welcome:not([hidden])')

    return [*[(name, tab(h)) for name, h in base.tabs(page)], ('Household', household), ('What did I miss', missed)]


class OwnerFleet(unittest.TestCase):
    maxDiff = None

    def each_view(self, check, widths=WIDTHS, themes=base.THEMES):
        problems, errors = {}, {}
        for width in widths:

            for theme in themes:
                ctx = base.new_context(base.ADMIN, width, theme)
                page = base.open_view(ctx)
                console = []
                page.on('console', lambda m, c=console: c.append(m.text) if m.type == 'error' else None)
                for name, open_it in views(page):
                    open_it()
                    found = check(page)
                    if found:
                        problems[f'{name} {width} {theme}'] = found
                    base.shot(page, f'owner-{name.lower().replace(" ", "-")}-{width}-{theme}')
                if page.errors or console:
                    errors[f'{width} {theme}'] = page.errors + console
                ctx.close()
        self.assertEqual(problems, {})
        self.assertEqual(errors, {}, 'script or console errors')

    def test_layout_headlines_and_copy_every_view_size_and_theme(self):
        self.each_view(lambda p: p.evaluate('[...tuttiChecks.layout(), ...tuttiChecks.headlinesFit(), ...tuttiChecks.copyLint()]'))

    def test_axe_every_view_size_and_theme(self):
        self.each_view(base.axe)

    def test_keyboard_reaches_the_tile_details_with_visible_focus(self):
        ctx = base.new_context(base.ADMIN)
        page = base.open_view(ctx, '#dashboard')
        summary = page.locator('#tiles details > summary').first
        summary.focus()
        self.assertTrue(page.evaluate('tuttiChecks.focusShown()'))
        page.keyboard.press('Enter')
        self.assertTrue(summary.evaluate('(s) => s.parentElement.open'))
        ctx.close()

    def test_the_quota_tile_counts_and_lists_the_accounts_in_its_details(self):
        ctx = base.new_context(base.ADMIN)
        page = base.open_view(ctx, '#dashboard')
        quota = page.locator('.tile', has_text='Quota')
        self.assertEqual(quota.locator('.tile__value').text_content(), '4 accounts at the limit')
        self.assertIn('agy-claude, agy-claude-weekly, agy-gemini, claude', quota.locator('details').text_content())
        jobs = page.locator('.tile', has_text='Jobs')
        self.assertIn('3 stalled: open their logs', jobs.text_content())
        ctx.close()

    def test_the_config_error_names_the_file_the_problem_and_the_fix(self):
        ctx = base.new_context(base.ADMIN)
        page = base.open_view(ctx, '#dashboard')
        text = page.text_content('#banner')
        self.assertRegex(text, r'^Your config file \S+tutti\.config\.yaml has an error, so Tutti still uses the last version that worked\.')
        self.assertRegex(text, r'The problem: .+ at line \d+, column \d+\. To fix it, correct the file')
        base.go(page, '#settings')
        page.wait_for_selector('#settings-body [role=alert]')
        msg = page.text_content('#settings-body [role=alert]')
        self.assertIn('The settings did not load. Your config file', msg)
        self.assertNotIn('Failed to fetch', msg)
        ctx.close()

    def test_the_settings_tab_shows_loading_before_its_answer(self):
        ctx = base.new_context(base.ADMIN)
        ctx.route('**/api/settings', lambda r: None)   # never answers: the loading state stays
        page = base.open_view(ctx)
        base.go(page, '#settings')
        self.assertIn('Loading the settings', page.text_content('#settings-body'))
        ctx.close()
