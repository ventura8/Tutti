"""Local model workers (Ollama's HTTP API): ask, rewrite one file, or loop edit -> check -> retry. Bulky reading and drafting
costs no cloud tokens. A worker waits while its machine's yield flag is up, and a low-priority call lets other local jobs go first."""
import atexit
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error

from . import config, remote, waiting

# A simple fence for code blocks.
FENCE = re.compile(r'