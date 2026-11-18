"""Mark a unit test as flaky: one automatic re-run on AssertionError (docs/LESSONS.md, *Dashboard free/busy*)."""
import functools


def flaky(fn):
    """Run `fn` once more after an AssertionError; any other exception, or a second AssertionError, propagates."""

    @functools.wraps(fn)
    def wrap(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except AssertionError:
            return fn(*args, **kwargs)

    wrap.flaky = True
    return wrap
