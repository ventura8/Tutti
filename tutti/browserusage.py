"""Read configured subscription meters from their visible, scoped usage pages."""
from . import browsercmd, helper


def read(cfg, url):
    """Read all account meters on one configured page through the Work browser's read-only extractor."""
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
