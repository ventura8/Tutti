"""Self-test the live Tutti instance: read-only viewer."""

import time
import traceback
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

# Need to make sure tests/e2e is in PYTHONPATH when this runs
from tests.e2e.harness import Dashboard
from . import backlog


LAST_RUN = 0


def run_self_test(cfg):
    """Run a live self-test of the instance, at most once per hour."""
    global LAST_RUN
    if time.time() - LAST_RUN < 3600:
        return {'status': 'skip', 'timestamp': datetime.now(timezone.utc).isoformat()}

    LAST_RUN = time.time()
    
    # Assuming standard setup
    dash = Dashboard(base=f'http://127.0.0.1:{cfg["dashboard"]["port"]}')

    results = {'timestamp': datetime.now(timezone.utc).isoformat(), 'status': 'ok', 'errors': []}

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            # Viewer identity, read-only
            ctx = browser.new_context(storage_state=None, service_workers='block')

            try:
                # E2E test: check for dashboard tabs
                page = ctx.new_page()
                page.goto(dash.base)
                # Check for key elements
                page.wait_for_selector('nav.tabs', timeout=5000)
                # Add more checks if needed here, keep it fast!
                
            except Exception as e:
                results['status'] = 'fail'
                results['errors'].append(str(e))
                # Add to backlog
                data = backlog.load(cfg)
                task = backlog.new_task('tutti', f'Live E2E self-test failed: {str(e)[:50]}', size='S', source='selftest')
                backlog.add(data, task)
                backlog.save(cfg, data)
            finally:
                ctx.close()
                browser.close()
    except Exception as e:
        results['status'] = 'fail'
        results['errors'].append(f"Infrastructure error: {str(e)}")
        # Add to backlog
        data = backlog.load(cfg)
        task = backlog.new_task('tutti', f'Live E2E self-test infra error: {str(e)[:50]}', size='S', source='selftest')
        backlog.add(data, task)
        backlog.save(cfg, data)

    # Store results (maybe in state dir)
    return results
