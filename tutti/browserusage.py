"""Read configured subscription meters from their visible, scoped usage pages."""
from . import browsercmd, helper

OFF = ('reading usage from a web page is off (owner, 2026-11-23): Tutti reads usage only through a provider\'s API or '
       'from the usage notes agents send with their messages; usage_url is a link for people only')


def read(cfg, url):
    """Formerly read meters from a usage page in the Work browser; now always refused (OFF): no usage page is opened."""
    raise ValueError(OFF)


def _read_page(cfg, url):
    """The old page reader, kept unreachable for reference until it is deleted with the browser usage helper."""
    targets = helper.helper_pages(cfg).get(url)
    if not targets:
        raise ValueError(f'{url} is not a configured usage page: add accounts.<name>.usage_url')
    result = browsercmd.broker(cfg).page('usage-reading', url, 'usage', 'tutti usage reader')
    if result.get('signed_out'):
        raise ValueError(f'{url.split("/")[2]} needs a sign-in in the Work browser: sign in once from Machines, Work browser')
    value = result.get('value') or {}
    rows = value.get('readings')
    if not isinstance(rows, list):
        raise ValueError('the Work browser returned no usage readings: check its sign-in and the page labels')
    readings = {row['account']: {'used': row.get('used'), 'resets': row.get('resets')}
                for row in rows if isinstance(row, dict) and isinstance(row.get('account'), str)}
    return {'readings': readings, 'missing': value.get('missing') or [], 'loading': bool(value.get('loading'))}
