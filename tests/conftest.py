"""Pytest session defaults that match scripts/check.sh's isolation: never the owner's grok.env or daemons."""
import atexit
import os
import shutil
import tempfile

os.environ.setdefault('TUTTI_TELEMETRY', 'off')
os.environ.setdefault('TUTTI_OFFLINE', '1')
os.environ.setdefault('TUTTI_NO_DAEMONS', '1')
# Empty XDG so secretenv/grok.env never comes from the owner's ~/.config (scripts/check.sh uses a temp dir too).
if not os.environ.get('XDG_CONFIG_HOME'):
    _xdg = tempfile.mkdtemp(prefix='tutti-pytest-xdg-')
    os.environ['XDG_CONFIG_HOME'] = _xdg
    atexit.register(shutil.rmtree, _xdg, ignore_errors=True)
