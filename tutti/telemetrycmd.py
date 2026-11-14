"""The CLI's telemetry: the entry wrapper that records every command (name, flag names, duration, outcome) and reports a crash,
the one-time consent question, and `tutti telemetry on|off|status|setup|report|send`. docs/TELEMETRY.md."""
import getpass
import json
import os
import sys
import time

from . import config, insights, scrub, sentry, telemetry

# Commands that are plumbing inside other commands or scripts: never interrupted by the consent question.
NO_PROMPT = ('telemetry', 'rsh', 'dashboard', 'wait', 'quiet', 'progress', 'job', 'handoff', 'conduct', 'run', 'test', 'guard',
             'tunnel', 'autopilot', 'scout', 'pr', 'ship')
QUESTION = ('Tutti can send anonymous crash reports and usage statistics (command names, durations, outcomes; never your '
            'prompts, tasks, code, paths, host names or keys; docs/TELEMETRY.md) to help make Tutti better. Everything is '
            'recorded on this computer either way. Send them? [y/N] ')
# Exit codes that are answers, not failures: 124/125 a wait's timeout or stall, 130 an interrupt.
NOT_FAILURES = (0, 124, 125, 130)


def split_argv(argv):
    """(the --config value, the words after the global options)."""
    path, rest, i = None, [], 0
    while i < len(argv):
        w = argv[i]
        if w == '--config' and i + 1 < len(argv):
            path, i = argv[i + 1], i + 2
            continue
        if w.startswith('--config='):
            path = w.split('=', 1)[1]
        else:
            rest.append(w)
        i += 1
    return path, rest


def flag_names(words):
    """The option names a command was given (`--bg`, `--repo`), never their values or the positional words."""
    names = []
    for w in words:
        if w == '--':
            break
        if w.startswith('-') and len(w) > 1 and not w[1:2].isdigit():
            n = w.split('=', 1)[0]
            names.append(n if scrub.CATEGORY_RX.fullmatch(n) else scrub.OTHER)
    return sorted(set(names))[:20]


def subcommand(p, words):
    """(command, action): the action only when it is one of the subcommand's fixed choices (`telemetry on`, `backlog add`)."""
    positional = [w for w in words if not w.startswith('-')]
    if not positional:
        return '', ''
    name = positional[0]
    subs = [a for a in p._actions if a.__class__.__name__ == '_SubParsersAction']
    sp = subs[0].choices.get(name) if subs else None
    if sp is None:
        return scrub.OTHER, ''
    choices = set()
    for a in sp._actions:
        choices |= set(a.choices or ()) if isinstance(a.choices, (list, tuple, dict)) else set()
    action = positional[1] if len(positional) > 1 and positional[1] in choices else ''
    return name, action


def context(path, env=None):
    """(config or None, the state folder) without failing: telemetry must work when the config is broken too."""
    try:
        cfg = config.load(path, env=env)
        return cfg, config.state_dir(cfg)
    except Exception:   # a broken config is the command's to report; telemetry falls back to the default folder
        return None, config.expand(config.DEFAULTS['project']['state'])


def should_ask(folder, command, env=None, stdin=None, stdout=None):
    env = os.environ if env is None else env
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    if not folder or command in NO_PROMPT or env.get('CI') or env.get('TUTTI_NO_PROMPT'):
        return False
    if not (stdin.isatty() and stdout.isatty()):
        return False
    return telemetry.consent(folder)['send'] is None


def ask_once(folder, read=input, say=None):
    """The one consent question; any answer but yes is no. Recorded, never asked again (`tutti telemetry on|off`)."""
    say = say or (lambda m: print(m, file=sys.stderr))
    try:
        answer = read(QUESTION).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ''
    on = answer in ('y', 'yes')
    telemetry.set_consent(folder, on, 'first-run')
    say('Sending is on; change it with `tutti telemetry off`.' if on else
        'Nothing will be sent; local recording stays on. Change it with `tutti telemetry on`.')
    return on


def run(main_fn, argv=None, env=None, clock=time.monotonic):
    """The `tutti` program: main() with every command recorded and an uncaught exception reported, then re-raised."""
    from . import cli
    argv = list(sys.argv[1:] if argv is None else argv)
    path, words = split_argv(argv)
    name, action = subcommand(cli.parser(), words)
    cfg, state_dir = context(path, env)
    folder = telemetry.configure(state_dir, cfg, name, env)
    if should_ask(folder, name, env):
        ask_once(folder)
    started, rc, outcome = clock(), 1, 'ok'
    try:
        rc = main_fn(argv)
        return rc
    except SystemExit as e:
        rc = exit_code(e.code)
        outcome = 'usage' if rc == 2 else outcome
        raise
    except KeyboardInterrupt:
        rc, outcome = 130, 'interrupted'
        raise
    except BaseException as e:
        rc, outcome = 1, 'crash'
        telemetry.capture_exception(type(e), e, e.__traceback__, where='cli')
        raise
    finally:
        finish(folder, cfg, name, action, words, rc, outcome, clock() - started)


def exit_code(code):
    """SystemExit's code as a number: None is 0, a message is 2 (argparse's usage error)."""
    if isinstance(code, int):
        return code
    return 0 if code is None else 2


def finish(folder, cfg, name, action, words, rc, outcome, seconds):
    if not folder:
        return
    rc = rc if isinstance(rc, int) else 0
    if outcome == 'ok' and rc not in (0, None):
        outcome = 'failed'
    telemetry.record('command', name or 'none', action=action, flags=flag_names(words), seconds=round(seconds, 3), rc=rc,
                     outcome=outcome)
    if outcome == 'failed' and rc not in NOT_FAILURES:
        telemetry.capture_failure(name or 'none', rc, where='cli', action=action)
    try:
        telemetry.flush(folder, cfg, force=outcome == 'crash')
    except Exception:   # the command's own result matters more than a send
        return


# ---- `tutti telemetry ...`

def status_lines(st):
    send = {True: 'on', False: 'off', None: 'off (never asked)'}[st['send']]
    target = 'none: run `tutti telemetry setup`'
    if st['endpoint_set']:
        target = 'a custom endpoint (telemetry.endpoint)'
    if st['dsn_set']:
        target = f'Sentry at {st["dsn_host"]} (DSN from {st["dsn_from"]})'
    lines = [f'sending: {send}', f'send to: {target}', f'local events: {st["events"]} (always recorded)',
             f'queued to send: {st["queued"]}; sent so far: {st["sent_total"]}']
    if st.get('last_error'):
        lines.append(f'last send problem: {st["last_error"]}')
    return lines


def setup(folder, read_secret=getpass.getpass, say=print, env=None):
    """Asks for the DSN (hidden input), checks its shape, keeps it in ~/.config/tutti/telemetry.env (600)."""
    say('Paste the DSN of the Sentry project for Tutti (Sentry: Settings, Projects, <project>, Client Keys (DSN)).')
    say(f'It is kept in {telemetry.env_file(env)} (readable by you only), never in a config file or a repository.')
    value = read_secret('DSN: ').strip()
    if not value:
        say('nothing pasted: nothing changed')
        return 2
    try:
        path = telemetry.save_dsn(value, env)
    except sentry.DsnError as e:
        say(f'not saved: {e}')
        return 2
    say(f'saved in {path}. Sending is {"on" if telemetry.sending(folder) else "off: turn it on with `tutti telemetry on`"}.')
    say('Test it with `tutti telemetry send --test`: one test event appears in the Sentry project.')
    return 0


def send_test(cfg, folder, post_fn=None):
    """One harmless test event, straight to the target (consent is the owner's own command here)."""
    t = sentry.target(cfg)
    if not t:
        print('no DSN or endpoint: run `tutti telemetry setup` first')
        return 2
    ev = telemetry.message_event('info', 'tutti telemetry test', {'where': 'test'})
    ev['user'] = {'id': telemetry.consent(folder)['install_id']}
    sent, _left = sentry.send_all(t, [{'type': 'event', 'event': ev}], ev['user']['id'], post_fn)
    print('test event sent: look for "tutti telemetry test" in the project' if sent else
          'the test event was not accepted: check the DSN (tutti telemetry setup) and the network')
    return 0 if sent else 1


def cmd(cfg, a):
    folder = telemetry.folder_of(config.state_dir(cfg))
    if a.action in ('on', 'off'):
        telemetry.set_consent(folder, a.action == 'on', 'cli')
        print(f'telemetry sending: {a.action}' + ('' if a.action == 'off' or sentry.target(cfg) else
                                                  ' (no DSN yet: run `tutti telemetry setup`)'))
        return 0
    if a.action == 'setup':
        return setup(folder)
    if a.action == 'report':
        s = insights.summary(telemetry.read_events(folder), a.days)
        print(json.dumps(s, indent=1) if a.json else '\n'.join(insights.lines(s)))
        return 0
    if a.action == 'send':
        if a.test:
            return send_test(cfg, folder)
        r = telemetry.flush(folder, cfg, force=True)
        print(f'sent {r["sent"]}; {r.get("left", 0)} left: {r["why"]}')
        return 0
    print('\n'.join(status_lines(telemetry.status(folder, cfg))))
    return 0
