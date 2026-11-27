"""Pytest session defaults that match scripts/check.sh's isolation: never the owner's grok.env or daemons."""
import atexit
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

os.environ.setdefault('TUTTI_TELEMETRY', 'off')
os.environ.setdefault('TUTTI_OFFLINE', '1')
os.environ.setdefault('TUTTI_NO_DAEMONS', '1')
# Autopilot runs set TUTTI_BACKGROUND=1; tests that spawn `tutti test` must not inherit it (class:background in .target).
os.environ.pop('TUTTI_BACKGROUND', None)
# Always an empty XDG (overwrite): secretenv must never see the owner's ~/.config/tutti/grok.env.
# scripts/check.sh does the same with XDG_CONFIG_HOME="$(mktemp -d)" around tests/run.py.
_xdg = tempfile.mkdtemp(prefix='tutti-pytest-xdg-')
os.environ['XDG_CONFIG_HOME'] = _xdg
atexit.register(shutil.rmtree, _xdg, ignore_errors=True)
