"""Compact output for an agent to read: the end of a log with its noise taken out, so a retry's brief, a handoff note or a
command's answer to a local model costs fewer tokens and says the same thing. Pure: text in, text out.

What goes: colour codes, carriage-return progress redraws, Tutti's own stderr echo (`tutti: X` written next to `X`), blank
lines, and runs of the same line (kept once, with how many times it came). What stays: every distinct line, in order; when
the result is still too long, the lines that look like an error first, then the end."""
import re

ANSI = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07')
ECHO = 'tutti: '
ERRORISH = re.compile(r'error|fail|exception|traceback|assert|denied|refused|not found|exited [1-9]|rc=[1-9]|\[failure\]', re.I)
# a line that differs from the one before only in its digits (a counter, a time) is the same message again
DIGITS = re.compile(r'\d+')


def clean_lines(text):
    """The lines of `text` without colour codes, progress redraws (the last state after a \\r), blank lines and Tutti's echo."""
    out = []
    for raw in ANSI.sub('', text or '').split('\n'):   # not splitlines(): it splits at \r too
        line = raw.rstrip('\r').rsplit('\r', 1)[-1].rstrip()   # a CRLF line ends in \r: that is not a redraw
        if not line.strip():
            continue
        if line.startswith(ECHO) and out and out[-1] == line[len(ECHO):]:
            continue
        if out and out[-1].startswith(ECHO) and out[-1][len(ECHO):] == line:
            out[-1] = line
            continue
        out.append(line)
    return out


def squeeze(lines):
    """Runs of the same line (digits aside) kept once, with `(xN)` when it came N times in a row."""
    out, counts = [], []
    for line in lines:
        if out and DIGITS.sub('#', out[-1]) == DIGITS.sub('#', line):
            counts[-1] += 1
            out[-1] = line   # the newest numbers: the last time, the last count
            continue
        out.append(line)
        counts.append(1)
    return [f'{line}  (x{n})' if n > 1 else line for line, n in zip(out, counts, strict=True)]


def fit(lines, max_chars):
    """At most `max_chars`: the end of the output, with the error-looking lines before it kept first when they would be cut."""
    text = '\n'.join(lines)
    if len(text) <= max_chars:
        return text
    tail, size = [], 0
    for line in reversed(lines):
        if size + len(line) + 1 > max_chars // 2:
            break
        tail.insert(0, line)
        size += len(line) + 1
    head = [ln for ln in lines[:len(lines) - len(tail)] if ERRORISH.search(ln)]
    kept, size = [], 0
    for line in reversed(head):   # the errors nearest the end matter most
        if size + len(line) + 1 > max_chars - max_chars // 2 - 40:
            break
        kept.insert(0, line)
        size += len(line) + 1
    skipped = len(lines) - len(tail) - len(kept)
    return '\n'.join([*kept, f'[... {skipped} line(s) left out ...]', *tail] if skipped else [*kept, *tail])[-max_chars:]


def compact(text, max_chars=3000):
    """The output an agent reads: cleaned, repeats squeezed, at most `max_chars`."""
    return fit(squeeze(clean_lines(text)), max_chars)


def tokens(text):
    """A rough token count (about four characters a token), for before/after measurements; never billed on."""
    return (len(text or '') + 3) // 4
