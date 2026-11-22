"""`tutti scout`: list, setup, ask, answer, watch and tick. The work is in scouts.py; this file reads the arguments, the
account states and stdin, and prints."""
import sys
import time
from datetime import datetime, timezone

from . import backlog, grokpatch, review, scouts, state, trial
from .review import gh


def accounts_now(cfg):
    return {s['name']: s for s in state.usage_state(cfg, datetime.now(timezone.utc))['accounts']}


def setup_text(cfg, name=None):
    names = [name] if name else list(cfg.get('scouts') or {})
    lines = ['Set up a Grok Bot scout (once, by the owner; Tutti never sees the key except through the variable):']
    lines += [f'{i}. {step}' for i, step in enumerate(scouts.SETUP, 1)]
    for n in names:
        sc = scouts.settings(cfg, n)
        gone = scouts.missing_env(sc)
        lines.append(f'scout {n}: url_env={sc["url_env"]}, key_env={sc["key_env"]}, answers in {sc["reply"]["github_repo"]} '
                     f'({"ready" if not gone else "missing: " + ", ".join(gone)})')
    return '\n'.join(lines)


def ask(cfg, a, gh_fn=gh, post_fn=scouts.post, stdin=None):
    sc = scouts.settings(cfg, a.scout)
    if a.skill and a.skill not in scouts.SKILLS:
        raise scouts.ScoutError(f'--skill {a.skill}: use one of {", ".join(scouts.SKILLS)}')
    if not a.queue:
        scouts.guard(cfg, sc)
    with scouts.locked(cfg) as data:
        r = scouts.new_request(data, sc, a.task, repo=a.repo, skill=a.skill)
        if a.queue:
            print(f'{r["id"]} queued: it goes out when {sc.get("account") or "the scout"} has room (the dashboard\'s round or '
                  f'`tutti scout tick`)')
            return 0
        key_in = (stdin or sys.stdin).readline() if a.key_stdin else None
        url, key = scouts.secrets(sc, key_stdin=key_in)
        scouts.send(sc, r, url, key, gh_fn, post_fn)
    if r['status'] == 'failed':
        print(f'{r["id"]} failed: {r["why"]}')
        return 1
    print(f'{r["id"]} sent: {r["url"]}')
    return 0 if a.no_wait else answer(cfg, r['id'], a.timeout, gh_fn)


def answer(cfg, rid, timeout=None, gh_fn=gh, sleep=time.sleep):
    """Waits up to `timeout` minutes (default: the scout's timeout_minutes; 0: look once) for the answer of a request."""
    with scouts.locked(cfg) as data:
        r = scouts.find(data, rid)
        sc = scouts.settings(cfg, r['scout'])
    if r['status'] == 'waiting':
        minutes = sc['timeout_minutes'] if timeout is None else timeout
        print(f'{rid}: waiting up to {minutes:g} min for the answer on {r["url"]}', flush=True)
        scouts.wait(sc, r, minutes * 60, gh_fn, sleep=sleep)
        with scouts.locked(cfg) as data:
            scouts.find(data, rid).update(r)
    if r['status'] == 'answered':
        print(f'{rid} answered by {r.get("answered_by") or "the scout"} ({r["url"]}):\n\n{r["answer"]}')
        return 0
    if r['status'] == 'waiting':
        print(f'{rid}: no answer yet on {r["url"]}; the routine may still be running (Grok Bot\'s chat shows it). Look again with '
              f'`tutti scout answer {rid}`')
        return 3
    print(f'{rid} {r["status"]}: {r.get("why") or "not sent yet"}')
    return 1


def watch(cfg, a, gh_fn=gh, post_fn=scouts.post):
    if a.what in (None, 'list'):
        print(scouts.describe(scouts.view(cfg, accounts_now(cfg))))
        return 0
    sc = scouts.settings(cfg, a.what)
    if not a.watch or a.watch not in sc['watches']:
        raise scouts.ScoutError(f'scout {a.what} has no watch "{a.watch}" (its watches: {", ".join(sc["watches"]) or "none"})')
    w = sc['watches'][a.watch]
    scouts.guard(cfg, sc)
    url, key = scouts.secrets(sc)
    with scouts.locked(cfg) as data:
        r = scouts.new_request(data, sc, w['task'], 'watch', a.watch, w.get('repo'), w.get('skill'))
        scouts.send(sc, r, url, key, gh_fn, post_fn)
    print(f'{r["id"]} {r["status"]}: {r.get("url") if r["status"] == "waiting" else r.get("why")}')
    return 0 if r['status'] == 'waiting' else 1


def _ask(cfg, a, gh_fn, post_fn):
    if not a.scout or not a.task:
        raise scouts.ScoutError('usage: tutti scout ask <scout> "<question>" [--repo owner/name] [--no-wait | --queue]')
    return ask(cfg, a, gh_fn, post_fn)


def _answer(cfg, a, gh_fn, _post_fn):
    if not a.scout:
        raise scouts.ScoutError('usage: tutti scout answer <id> (the id `tutti scout ask` printed, e.g. s3)')
    return answer(cfg, a.scout, a.timeout, gh_fn)


def _watch(cfg, a, gh_fn, post_fn):
    a.what, a.watch = a.scout, a.task
    return watch(cfg, a, gh_fn, post_fn)


def _tick(cfg, _a, gh_fn, post_fn):
    for e in scouts.tick(cfg, accounts_now(cfg), gh_fn=gh_fn, post_fn=post_fn) or ['nothing to send or collect']:
        print(f'scout: {e}')
    return 0


def _list(cfg, _a, _gh_fn, _post_fn):
    print(scouts.describe(scouts.view(cfg, accounts_now(cfg))))
    return 0


def _setup(cfg, a, gh_fn, post_fn):
    if a.scout == 'grok' or a.task:   # `setup grok [<name>]`: the guided, interactive setup in a real terminal
        from . import groksetup
        return groksetup.run(cfg, a.task or 'grok', gh_fn=gh_fn, post_fn=post_fn)
    print(setup_text(cfg, a.scout))
    return 0


def run(cfg, a, gh_fn=gh, post_fn=scouts.post):
    actions = {'list': _list, 'setup': _setup, 'ask': _ask, 'answer': _answer, 'watch': _watch, 'tick': _tick,
               'review': review_pr, 'trial': lambda c, x, g, _p: trial_cmd(c, x, g), 'patch': _patch, 'apply': _apply}
    return actions[a.action](cfg, a, gh_fn, post_fn)


def _patch(cfg, a, gh_fn=gh, post_fn=scouts.post):
    """`tutti scout patch <task> [--via <scout>]`: the scout drafts the task as one diff (t206, docs/SCOUTS.md, *Patches*)."""
    if not a.scout:
        raise scouts.ScoutError('usage: tutti scout patch <task id> [--via <scout>]')
    name = scout_name(cfg, a.via)
    failure = ((backlog.find(backlog.load(cfg), a.scout).get('patch') or {}).get('failure'))
    try:
        r = grokpatch.ask(cfg, a.scout, name, gh_fn, post_fn, failure=failure)
    except grokpatch.PatchError as e:
        raise scouts.ScoutError(str(e)) from e
    print(f'{r["id"]} sent: {name} drafts {a.scout} as one diff ({r.get("url")}). The scout round keeps its diff; then '
          f'`tutti scout apply {a.scout}`' + (' (second try, with the failing output)' if failure else ''))
    return 0


def _apply(cfg, a, _gh_fn=gh, _post_fn=scouts.post):
    """`tutti scout apply <task>`: the scout's diff applied at its base in a worktree of its own, then the check."""
    if not a.scout:
        raise scouts.ScoutError('usage: tutti scout apply <task id>')
    try:
        ok, wt, out = grokpatch.apply(cfg, a.scout)
    except grokpatch.PatchError as e:
        raise scouts.ScoutError(str(e)) from e
    print(out[-2000:])
    print(f'{a.scout}: {"the check passed" if ok else "the check failed"} in {wt}'
          + (f' (branch grok/{a.scout}: review and land it)' if ok else ''))
    return 0 if ok else 1


def scout_name(cfg, via, want_trial=False):
    """--via, else the only scout (with a review trial, when `want_trial`)."""
    if via:
        scouts.settings(cfg, via)
        return via
    names = [n for n in cfg.get('scouts') or {} if not want_trial or trial.settings(cfg, n) or n in trial.load(cfg)['scouts']]
    if len(names) != 1:
        raise scouts.ScoutError(f'name the scout with --via ({", ".join(names) or "no scout configured: docs/SCOUTS.md"})')
    return names[0]


def review_pr(cfg, a, gh_fn=gh, post_fn=scouts.post):
    """`tutti scout review <pr>`: the scout reviews a pull request and posts its findings there, tagged grok-scout. Advisory."""
    if not a.scout or not a.scout.isdigit():
        raise scouts.ScoutError('usage: tutti scout review <pr number> [--repo owner/name] [--via <scout>]')
    name, repo = scout_name(cfg, a.via), review.repo_of(a.repo, gh_fn)
    rc, url, _err = gh_fn(['pr', 'view', a.scout, '--repo', repo, '--json', 'url', '--jq', '.url'])
    r = trial.ask_review(cfg, name, repo, int(a.scout), url.strip() if rc == 0 else f'https://github.com/{repo}/pull/{a.scout}',
                         gh_fn, post_fn)
    if r['status'] == 'failed':
        print(f'{r["id"]} failed: {r["why"]}')
        return 1
    print(f'{r["id"]} sent: {name} reviews {repo}#{a.scout}; its comment starts with "{scouts.REVIEW_MARKER}" (advisory, never '
          f'blocking). `tutti scout answer {r["id"]}` waits for it')
    return 0


def trial_cmd(cfg, a, gh_fn=gh):
    """`tutti scout trial [status|collect|show|mark|done] [owner/name#n] [<finding> real|noise]`."""
    sub, key, rest = a.scout or 'status', a.task, a.rest or []
    name = scout_name(cfg, a.via, want_trial=True)
    if sub == 'status':
        print(trial.describe([v for v in trial.view(cfg) if v['scout'] == name]))
        return 0
    if not key:
        raise scouts.ScoutError(f'usage: tutti scout trial {sub} owner/name#number' + (' <finding> real|noise' if sub == 'mark' else ''))
    if sub == 'collect':
        trial.collect(cfg, name, key, gh_fn)
        print(trial.show(cfg, name, key))
    elif sub == 'show':
        print(trial.show(cfg, name, key))
    elif sub == 'mark':
        if len(rest) != 2:
            raise scouts.ScoutError(f'usage: tutti scout trial mark {key} <finding id, e.g. f2> real|noise')
        f = trial.mark(cfg, name, key, rest[0], rest[1])
        print(f'{key} {f["id"]} [{f["reviewer"]}]: {f["verdict"]}')
    elif sub == 'done':
        e = trial.close(cfg, name, key)
        print(f'{key}: done' + (f'; trial verdict: {e["verdict"]["why"]}' if e['verdict'] else ''))
    else:
        raise scouts.ScoutError(f'trial {sub}: use status, collect, show, mark or done')
    return 0
