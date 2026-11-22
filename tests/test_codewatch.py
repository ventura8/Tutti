"""A running dashboard whose code changed on disk restarts itself (the pages are read live, so stale code behind new pages
broke every new tab): it waits while Chat is busy and never restarts for an unchanged tree."""
import os
import tempfile
import threading
import time
import unittest

from tutti import install


class Inline:
    """A thread stand-in that runs its target at once."""

    def __init__(self, target, **_kw):
        self.target = target

    def start(self):
        self.target()


class CodeWatch(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = d.name
        os.makedirs(os.path.join(self.root, 'tutti', 'web'))
        self.py = os.path.join(self.root, 'tutti', 'a.py')
        open(self.py, 'w').close()

    def test_the_fingerprint_follows_the_python_code_only(self):
        before = install.code_fingerprint(self.root)
        open(os.path.join(self.root, 'tutti', 'web', 'x.js'), 'w').close()
        self.assertEqual(install.code_fingerprint(self.root), before)
        os.utime(self.py, (time.time() + 5, time.time() + 5))
        self.assertNotEqual(install.code_fingerprint(self.root), before)
        open(os.path.join(self.root, 'tutti', 'b.py'), 'w').close()
        self.assertEqual(install.code_fingerprint(self.root)[1], 2)

    def test_new_code_restarts_once_chat_is_idle_and_unchanged_code_never(self):
        prints = iter([(1, 1), (1, 1), (2, 1), (2, 1), (2, 1)])
        answers, calls = iter([False, None]), []
        stop = threading.Event()

        def restart():
            calls.append(1)
            return next(answers)
        install.start_code_watch(stop, self.root, restart, every=0, fingerprint=lambda r: next(prints), thread=Inline)
        self.assertEqual(len(calls), 2)   # deferred once (Chat busy), then done
        stop.set()
        calls.clear()
        install.start_code_watch(stop, self.root, restart, every=0, fingerprint=lambda r: (1, 1), thread=Inline)
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
