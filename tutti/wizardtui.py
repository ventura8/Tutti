"""The setup wizard in a terminal (`tutti setup --tui`; docs/SETUP-WIZARD.md): the same steps, fields and backend as the
dashboard's Setup view (tutti/wizard.py), keyboard only.

Two faces over one pure Form: a full-screen one (curses, the standard library) where the cursor always sits on the line in
focus, so a screen reader follows it; and a plain one (`--plain`, or wherever curses cannot start: Windows without it, a
dumb terminal) that asks one numbered question at a time and prints every answer, the friendliest for a screen reader.
Sign-ins run in the tool's own program (Tutti never sees a credential)."""
import os
import subprocess
import sys

from . import config, wizard

MARK = {'done': 'done', 'needs-you': 'needs you', 'optional': 'optional', 'skipped': 'skipped', 'locked': 'admin only'}
STATE = {'ok': 'ok', 'todo': 'needs you', 'info': 'note'}


def has_browser(env=None, platform=None):
    """Whether a browser can open the dashboard here: a desktop session (Windows, macOS, X11, Wayland) or $BROWSER."""
    env = os.environ if env is None else env
    platform = platform or sys.platform
    if platform in ('win32', 'darwin') or env.get('BROWSER'):
        return True
    return bool(env.get('DISPLAY') or env.get('WAYLAND_DISPLAY'))


def initial(fields):
    """{key: value} of a step's prefilled fields."""
    return {f['key']: list(f['value']) if f['kind'] == 'multi' else f['value'] for f in fields}


class Form:
    """One step's fields plus Apply, Skip and Back, as lines; keys move the focus and change values. Pure: tests drive it."""

    def __init__(self, step):
        self.step = step
        self.values = initial(step['fields'])
        self.focus = 0
        self.sub = 0   # the option in focus inside a choice or multi field
        self.typing = None   # the text being typed into a number or text field

    def controls(self):
        """[(kind, field or action)]: the fields, then the actions."""
        acts = [] if self.step['locked'] else ['apply'] + ([] if self.step['id'] in ('welcome', 'review') else ['skip'])
        return [('field', f) for f in self.step['fields']] + [('action', a) for a in acts + ['back']]

    def current(self):
        return self.controls()[self.focus]

    def key(self, k):
        """Handles one key ('up', 'down', 'left', 'right', 'space', 'enter', 'backspace' or a character); returns an action
        name ('apply', 'skip', 'back') when one is chosen, else None."""
        kind, what = self.current()
        if self.typing is not None:
            return self.type_key(what, k)
        n = len(self.controls())
        if k in ('down', 'tab'):
            self.focus, self.sub = (self.focus + 1) % n, 0
        elif k in ('up', 'btab'):
            self.focus, self.sub = (self.focus - 1) % n, 0
        elif kind == 'action':
            return what if k in ('enter', 'space') else None
        else:
            self.field_key(what, k)
        return None

    def field_key(self, f, k):
        opts = f['options']
        if f['kind'] in ('choice', 'multi') and k in ('left', 'right') and opts:
            self.sub = (self.sub + (1 if k == 'right' else -1)) % len(opts)
            if f['kind'] == 'choice':
                self.values[f['key']] = opts[self.sub]['value']
        elif f['kind'] == 'multi' and k in ('space', 'enter') and opts:
            v, pick = self.values[f['key']], opts[self.sub]['value']
            self.values[f['key']] = [x for x in v if x != pick] if pick in v else [*v, pick]
        elif f['kind'] == 'bool' and k in ('space', 'enter', 'left', 'right'):
            self.values[f['key']] = not self.values[f['key']]
        elif f['kind'] in ('number', 'text') and k == 'enter':
            self.typing = str(self.values[f['key']])

    def type_key(self, f, k):
        if k == 'enter':
            text, self.typing = self.typing, None
            self.values[f['key']] = parse(f, text)
        elif k == 'escape':
            self.typing = None
        elif k == 'backspace':
            self.typing = self.typing[:-1]
        elif len(k) == 1 and k.isprintable():
            self.typing += k
        return None

    def lines(self):
        """[(text, focused)] of the whole step."""
        s = self.step
        out = [(f'{s["title"]} ({MARK.get(s["status"], s["status"])})', False), ('', False)]
        out += [(f'  [{STATE[r["state"]]}] {r["title"]}: {r["detail"]}' + (f'  $ {r["command"]}' if r['command'] else ''), False)
                for r in s['rows']]
        if s['locked']:
            out.append(('  Only an admin can change this step.', False))
        out.append(('', False))
        for i, (kind, what) in enumerate(self.controls()):
            on = i == self.focus
            out.append(((self.field_line(what, on) if kind == 'field' else f'< {what.capitalize()} >'), on))
        return out

    def field_line(self, f, on):
        v = self.values[f['key']]
        if on and self.typing is not None:
            return f'{f["label"]}: {self.typing}_  (Enter to keep, Esc to cancel)'
        if f['kind'] == 'bool':
            return f'[{"x" if v else " "}] {f["label"]}'
        if f['kind'] == 'choice':
            label = next((o['label'] + (f' ({o["note"]})' if o['note'] else '') for o in f['options'] if o['value'] == v), str(v))
            return f'{f["label"]}: < {label} >' + ('  (Left/Right to change)' if on else '')
        if f['kind'] == 'multi':
            o = f['options'][self.sub] if f['options'] else None
            picked = ', '.join(v) or 'none'
            mark = f'  [{"x" if o["value"] in v else " "}] {o["label"]} (Left/Right, Space)' if on and o else ''
            return f'{f["label"]}: {picked}{mark}'
        return f'{f["label"]}: {v}' + ('  (Enter to edit)' if on else '')


def parse(f, text):
    if f['kind'] == 'number':
        try:
            return int(text.strip())
        except ValueError:
            return text.strip()   # the engine says what is wrong with it
    return text.strip()


class Session:
    """The wizard's state between the two faces: the engine's view, a message, and the sign-in runner."""

    def __init__(self, path, detect_fn=None, run=subprocess.run, installer=wizard.install_failover):
        self.path, self.run, self.installer = path, run, installer
        self.detect_fn = detect_fn or wizard.detect
        self.found = self.detect_fn()
        self.message = ''
        self.refresh()

    def refresh(self):
        self.cfg = config.load(self.path)
        self.data = wizard.view(self.cfg, self.found)

    def step(self, sid):
        return next(s for s in self.data['steps'] if s['id'] == sid)

    def act(self, sid, action, values):
        """Applies or skips a step; the message says what happened or what is wrong."""
        try:
            self.message = wizard.apply(self.cfg, self.found, sid, {'skip': True} if action == 'skip' else values, self.run, self.installer)
        except wizard.WizardError as e:
            self.message = f'Not applied: {e}'
            return False
        self.found = self.detect_fn()
        self.refresh()
        return True

    def sign_in(self, command):
        """The tool's own sign-in, in this terminal (the person types into it, not into Tutti)."""
        self.run(command.split(), check=False)
        self.found = self.detect_fn()
        self.refresh()


# ---- the plain face: one numbered question at a time


def plain(session, read=input, out=sys.stdout):
    """The menu of steps, then each step's questions. Returns 0."""
    def say(text=''):
        print(text, file=out)
    while True:
        say('\nTutti setup wizard. Steps:')
        for i, s in enumerate(session.data['steps'], 1):
            say(f'  {i}. {s["title"]}: {MARK.get(s["status"], s["status"])}')
        cur = session.data['current']
        n = next(i for i, s in enumerate(session.data['steps'], 1) if s['id'] == cur)
        got = ask(read, f'Step number to open, Enter for {n} ({session.step(cur)["title"]}), q to quit', str(n))
        if got.lower() in ('q', 'quit'):
            say('Bye: run `tutti setup --tui` to come back; the wizard resumes where you left it.')
            return 0
        if not got.isdigit() or not 1 <= int(got) <= len(session.data['steps']):
            say(f'"{got}" is not a step number from 1 to {len(session.data["steps"])}.')
            continue
        plain_step(session, session.data['steps'][int(got) - 1], read, say)


def ask(read, question, default=''):
    try:
        got = read(f'{question}: ').strip()
    except EOFError:
        return 'q'
    return got or default


def plain_step(session, step, read, say):
    say(f'\n{step["title"]} ({MARK.get(step["status"], step["status"])})')
    for r in step['rows']:
        say(f'  [{STATE[r["state"]]}] {r["title"]}: {r["detail"]}' + (f'\n      command: {r["command"]}' if r['command'] else ''))
        if r['command'] and r['runnable'] and r['state'] == 'todo' and \
                ask(read, f'    Run "{r["command"]}" now (y/N)', 'n').lower().startswith('y'):
            session.sign_in(r['command'])
    if step['locked']:
        say('  Only an admin can change this step.')
        return
    values = initial(step['fields'])
    for f in step['fields']:
        values[f['key']] = plain_field(f, values[f['key']], read, say)
    choices = 'a = apply, s = skip, b = back' if step['id'] not in ('welcome', 'review') else 'a = apply, b = back'
    got = ask(read, f'{choices} (Enter: apply)', 'a').lower()
    if got.startswith(('a', 's')):
        session.act(step['id'], 'skip' if got.startswith('s') else 'apply', values)
        say(session.message)


def plain_field(f, value, read, say):
    if f['kind'] == 'bool':
        got = ask(read, f'  {f["label"]} (y/n, Enter: {"y" if value else "n"})', 'y' if value else 'n')
        return got.lower().startswith('y')
    if f['kind'] in ('choice', 'multi'):
        say(f'  {f["label"]}:')
        for i, o in enumerate(f['options'], 1):
            on = o['value'] in value if f['kind'] == 'multi' else o['value'] == value
            say(f'    {i}. {o["label"]}' + (f' ({o["note"]})' if o['note'] else '') + (' [chosen]' if on else ''))
        hint = 'numbers separated by commas, 0 for none' if f['kind'] == 'multi' else 'a number'
        got = ask(read, f'  Your choice ({hint}; Enter keeps the chosen)', '')
        if not got:
            return value
        nums = [int(x) for x in got.replace(' ', '').split(',') if x.isdigit() and 0 < int(x) <= len(f['options'])]
        picked = [f['options'][i - 1]['value'] for i in nums]
        if f['kind'] == 'multi':
            return [] if got.strip() == '0' else picked
        return picked[0] if picked else value
    got = ask(read, f'  {f["label"]} (Enter keeps "{value}")', '')
    return parse(f, got) if got else value


# ---- the full-screen face (curses)


KEYS = {'KEY_UP': 'up', 'KEY_DOWN': 'down', 'KEY_LEFT': 'left', 'KEY_RIGHT': 'right', 'KEY_BTAB': 'btab', '\t': 'tab', '\n': 'enter',
        '\r': 'enter', ' ': 'space', 'KEY_ENTER': 'enter', 'KEY_BACKSPACE': 'backspace', '\x7f': 'backspace', '\x1b': 'escape'}


def key_name(k):
    return KEYS.get(k, k)


def menu_lines(session, focus):
    out = [('Tutti setup wizard: Up/Down to choose a step, Enter to open, q to quit', False), ('', False)]
    for i, s in enumerate(session.data['steps']):
        out.append((f'{i + 1}. {s["title"]}: {MARK.get(s["status"], s["status"])}', i == focus))
    out += [('', False), (session.message, False)] if session.message else []
    return out


def draw(scr, lines):
    import curses
    scr.erase()
    h, w = scr.getmaxyx()
    at = None
    for y, (text, on) in enumerate(lines[:h - 1]):
        scr.addnstr(y, 0, ('> ' if on else '  ') + text, w - 1, curses.A_REVERSE if on else curses.A_NORMAL)
        at = y if on else at
    scr.move(at if at is not None else 0, 0)   # the cursor on the focused line: a screen reader reads it
    scr.refresh()


def full(scr, session):
    import curses
    curses.curs_set(1)
    focus = next(i for i, s in enumerate(session.data['steps']) if s['id'] == session.data['current'])
    while True:
        draw(scr, menu_lines(session, focus))
        k = key_name(scr.getkey())
        if k in ('q', 'escape'):
            return 0
        if k in ('up', 'down'):
            focus = (focus + (1 if k == 'down' else -1)) % len(session.data['steps'])
        elif k == 'enter':
            full_step(scr, session, session.data['steps'][focus]['id'])


def full_step(scr, session, sid):
    form = Form(session.step(sid))
    while True:
        draw(scr, form.lines() + ([('', False), (session.message, False)] if session.message else []))
        action = form.key(key_name(scr.getkey()))
        if action == 'back':
            return
        if action in ('apply', 'skip'):
            ok = session.act(sid, action, form.values)
            if ok:
                return
            form = Form(session.step(sid)) if action == 'skip' else form


def main(path, plain_mode=False, read=input, out=sys.stdout, detect_fn=None, run=subprocess.run):
    """`tutti setup --tui [--plain]`: 0 when the person leaves the wizard."""
    session = Session(path, detect_fn, run)
    if not plain_mode and sys.stdout.isatty() and os.environ.get('TERM', 'dumb') != 'dumb':
        try:
            import curses
            return curses.wrapper(full, session)
        except ImportError:
            print('the full-screen wizard needs curses (Windows: pip install windows-curses); here is the plain one', file=out)
    return plain(session, read, out)
