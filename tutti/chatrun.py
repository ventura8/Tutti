"""The dashboard's Chat, the I/O part (docs/CHAT.md): the answer jobs (a thread each, so an answer goes on when the page
closes), the models (Ollama's streaming chat, a CLI worker), the tools (the same calls as the MCP server's), Undo, the audit
log and the HTTP actions. tutti/chat.py holds the pure part."""
import json
import queue
import re
import subprocess
import threading
import time
import urllib.request
from datetime import datetime

from . import auth, chat, config, controls, mcpserver, workers

MODEL_TIMEOUT = 120      # seconds a model may take to start answering
CLI_TIMEOUT = 180        # seconds a CLI worker may take for a whole answer
ACTIVITY_SHOWN = 50
PREFIX = 'chat: '   # the chat's lines in the audit log
NO_ANSWER = ('I looked things up but did not reach an answer. Ask again, more specifically, or pick a more capable model '
             'above.')
ROUNDS = 4        # tool rounds a model may take for one answer
MAX_CALLS = 4     # tool calls taken from one round
WORK_ACTIONS = ('chat: Queue a task', 'chat: Hand off', 'chat: Ask ')   # audit lines that count against a viewer's daily limit
BUILTIN = 'Tutti (built-in, from the dashboard\'s state)'


# ---- the models


def ollama_stream(worker, messages, cancel, timeout=MODEL_TIMEOUT, tools=None):
    """Yields the answer's text pieces from Ollama's /api/chat (streamed), and {'tool_calls': [...]} when the model calls
    tools (Ollama's native function calling, each call checked against its schema before anything runs). OSError when the
    server does not answer."""
    payload = {'model': worker['model'], 'messages': messages, 'stream': True, 'think': False,
               'options': {'temperature': worker.get('temperature', 0.2), 'num_ctx': worker.get('num_ctx', 16384)}}
    if tools:
        payload['tools'] = tools
    req = urllib.request.Request(config.ollama_url(worker['host'], '/api/chat'), json.dumps(payload).encode(),
                                 {'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for line in r:
            if cancel.is_set():
                return
            try:
                part = json.loads(line)
            except ValueError:
                continue
            msg = part.get('message') or {}
            if msg.get('tool_calls'):
                yield {'tool_calls': msg['tool_calls']}
            if msg.get('content'):
                yield msg['content']
            if part.get('done'):
                return


def cli_stream(worker, messages, cancel, timeout=CLI_TIMEOUT, popen=subprocess.Popen, clock=time.monotonic):
    """Yields a CLI worker's answer line by line (`claude -p`, `cursor-agent -p`): the conversation as one prompt. A reader
    thread feeds a queue, so Stop and the deadline are seen even while the worker prints nothing; past the deadline the
    worker is killed and OSError lets the next model answer."""
    prompt = '\n\n'.join(f'{m["role"].upper()}:\n{m["content"]}' for m in messages) + '\n\nASSISTANT:'
    proc = popen(workers.command(worker, prompt), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                 text=True, start_new_session=True)
    lines = queue.Queue()
    threading.Thread(target=_pump, args=(proc.stdout, lines), daemon=True).start()
    deadline, name = clock() + timeout, (worker.get('command') or ['the worker'])[0]
    try:
        while not cancel.is_set():
            if clock() > deadline:
                raise OSError(f'{name} gave no full answer within {timeout} s')
            try:
                line = lines.get(timeout=0.25)
            except queue.Empty:
                continue
            if line is None:
                break
            yield line
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    if proc.returncode and not cancel.is_set():
        raise OSError(f'{name} ended with exit code {proc.returncode}')


def _pump(src, lines):
    for line in src:
        lines.put(line)
    lines.put(None)


def stream_of(cand, cfg, messages, cancel):
    w = dict((cfg.get('workers') or {}).get(cand['worker']) or {})
    if cand['kind'] == 'ollama':
        return ollama_stream(w, messages, cancel, tools=chat.ollama_tools() if cand.get('tools') else None)
    return cli_stream(w, messages, cancel)


# ---- the tools: the MCP server's own calls, after the chat's role check


def run_tool(cfg, call, runner=None):
    """(ok, text, undo) of one call. Undo is a call that takes it back, or None."""
    a = call['args']
    if call['tool'] == 'control':
        controls.set_paused(config.state_dir(cfg), a['kind'], a['name'], a['action'] == 'pause', source='chat')
        back = {'tool': 'control', 'args': {**a, 'action': 'resume' if a['action'] == 'pause' else 'pause'}}
        return True, f'{a["kind"].capitalize()} {a["name"]} is {"paused" if a["action"] == "pause" else "resumed"}.', back
    if call['tool'] == 'backlog_rm':
        rc, text = (runner or mcpserver.run_tutti)(cfg['_path'], ['backlog', 'rm', a['id']])
        return rc == 0, text, None
    err, text = mcpserver.call(cfg, call['tool'], a, 'admin', runner=runner)
    undo = None
    if not err and call['tool'] == 'backlog_add':
        m = re.match(r'(t\d+):\s*added', text)
        undo = {'tool': 'backlog_rm', 'args': {'id': m[1]}} if m else None
    return not err, text, undo


def job_ref(text):
    """The id the dashboard shows the work under (a backlog task, a delegated run, a test job, a scout request)."""
    m = re.search(r'\b(t\d+|test-\d+-[0-9a-f]+|\d{10}-\d+(?:-\d+)?|q\d+)\b', text or '')
    return m[1] if m else None


# ---- the answer jobs


class Job:
    """One answer being written: its events (`delta`, `card`, `done`) for any page that follows it, and its stop flag."""

    def __init__(self, sub, cid, msg):
        self.sub, self.cid, self.msg = sub, cid, msg
        self.cancel, self.cond, self.events, self.finished = threading.Event(), threading.Condition(), [], False

    def emit(self, kind, **data):
        with self.cond:
            self.events.append({'type': kind, **data})
            self.finished = self.finished or kind == 'done'
            self.cond.notify_all()

    def follow(self, start=0, wait=15.0):
        """Yields events from `start` until done; a keep-alive while nothing happens."""
        i = start
        while True:
            with self.cond:
                if i >= len(self.events) and not self.finished:
                    self.cond.wait(wait)
                batch, done = self.events[i:], self.finished
            if not batch and not done:
                yield {'type': 'ping'}
            yield from batch
            i += len(batch)
            if done and i >= len(self.events):
                return


class Engine:
    """The chat for one dashboard: the store, the live jobs, the snapshot and config it answers from."""

    def __init__(self, cfg_fn, snap_fn, folder=None, clock=time.time, runner=None, streams=None, owner_fn=None,
                 own_machine_fn=None):
        self.cfg_fn, self.snap_fn, self.clock, self.runner = cfg_fn, snap_fn, clock, runner
        self.folder = folder or config.state_dir(cfg_fn())
        self.store = chat.Store(self.folder, clock)
        self.streams = streams or stream_of
        self.owner_fn = owner_fn or (lambda: None)
        self.own_machine_fn = own_machine_fn or (lambda user: None)
        self.jobs, self.lock, self.act_lock = {}, threading.Lock(), threading.Lock()

    # -- reading

    def live_ids(self):
        with self.lock:
            return {j.msg['id'] for j in self.jobs.values() if not j.finished}

    def conversation(self, user, cid):
        data = self.store.interrupted(user['sub'], self.live_ids())
        return self.store.conversation(data, cid)

    def done_today(self, user):
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return sum(1 for e in auth.audit_log(self.folder, 10000) if e.get('by') == user['sub'] and e.get('at', 0) >= midnight
                   and str(e.get('action', '')).startswith(WORK_ACTIONS))

    def activity(self):
        """The household's chat actions (admins): who did what, from the audit log; never anyone's messages."""
        return [{'at': e.get('at'), 'by': e.get('by_email') or ('the owner' if e.get('by') == 'local' else 'a user'),
                 'action': e['action'][len(PREFIX):], 'detail': e.get('detail')}
                for e in auth.audit_log(self.folder, 1000) if str(e.get('action', '')).startswith(PREFIX)][:ACTIVITY_SHOWN]

    # -- sending

    def send(self, user, cid, text, attachments=(), edit=None, retry=None, model=None):
        """Starts an answer; returns its Job. `edit`: a user message id to replace (and what followed it); `retry`: an
        assistant message id to answer again."""
        sub = user['sub']
        if retry:
            conv = self.store.get(sub, cid)
            ids = [m['id'] for m in conv['messages']]
            if retry not in ids or ids.index(retry) == 0:
                raise chat.ChatError('That answer is gone: reload the chat.')
            prompt = conv['messages'][ids.index(retry) - 1]
            self.store.truncate_after(sub, cid, prompt['id'])
        else:
            text, attachments = chat.check_message(text, list(attachments or []))
            if not cid:
                cid = self.store.create(sub, model=model or self.store.load(sub).get('default_model') or 'auto')['id']
            if edit:
                self.store.truncate_after(sub, cid, edit, keep=False)
            prompt = {'id': chat.new_id('m'), 'role': 'user', 'text': text, 'attachments': attachments, 'at': chat.now_iso(self.clock),
                      'status': 'sent', 'edited': bool(edit)}
            self.store.put_message(sub, cid, prompt)
        answer = {'id': chat.new_id('m'), 'role': 'assistant', 'text': '', 'at': chat.now_iso(self.clock), 'status': 'streaming',
                  'model': None, 'cards': []}
        self.store.put_message(sub, cid, answer)
        job = Job(sub, cid, answer)
        job.emit('start', conversation=cid, prompt=prompt, message=answer)
        with self.lock:
            self.jobs = {k: j for k, j in self.jobs.items() if not j.finished}
            self.jobs[answer['id']] = job
        threading.Thread(target=self._answer, args=(job, user, prompt), daemon=True, name='tutti-chat').start()
        return job

    def stop(self, user, mid):
        with self.lock:
            job = self.jobs.get(mid)
        if job is None or job.sub != user['sub']:
            return False
        job.cancel.set()
        return True

    def _answer(self, job, user, prompt):
        msg = job.msg
        try:
            cfg, snap = self.cfg_fn(), self.snap_fn() or {}
            known = chat.known_names(cfg, snap)
            call = chat.parse(prompt.get('text') or '', known)
            if call and 'error' in call:
                self._say(job, call['error'], chat.ChatError)
            elif call:
                self._act(job, user, cfg, call)
            else:
                self._reply(job, user, cfg, snap, known)
            msg['status'] = 'stopped' if job.cancel.is_set() else 'done'
        except Exception as e:   # the page shows a plain message; the log line names the cause
            msg['status'] = 'failed'
            msg['note'] = ('Something went wrong while answering, so this answer is incomplete. Retry; if it fails again, the '
                           'dashboard\'s log names the cause.')
            job.emit('note', text=msg['note'])
            msg['error'] = f'{type(e).__name__}: {e}'[:300]
        finally:
            try:
                if job.cancel.is_set() and msg['status'] != 'failed':
                    msg['status'] = 'stopped'
                self.store.update_message(job.sub, job.cid, msg)   # a chat deleted or edited meanwhile is left as it is
            finally:
                job.emit('done', message=msg)   # always: a page following the answer must never wait forever

    def _say(self, job, text, _kind=None):
        job.msg['text'] = text
        job.msg['model'] = BUILTIN
        job.emit('model', model=BUILTIN)
        job.emit('delta', text=text)

    def execute(self, user, cfg, call, card):
        """Runs one call for `user` into `card`: the role check, the audit line, the MCP tool's call, Undo where possible."""
        with self.act_lock:   # the daily limit and its audit line together: two quick Confirms cannot both pass
            why = chat.refusal(user, call, cfg, self.done_today(user))
            if why:
                card.update(state='refused', text=why, call=None)
                return card
            target = chat.HOUSEHOLD if call['tool'] in chat.HOUSEHOLD_TOOLS else user
            try:
                auth.act_for(self.folder, user, target, PREFIX + card['label'][:80], detail=card['label'], clock=self.clock)
            except auth.AuthError:
                card.update(state='refused', text='Only an admin can do that.', call=None)
                return card
        ok, text, undo = run_tool(cfg, call, self.runner)
        card.update(state='done' if ok else 'failed', text=plain(text, ok), ref=job_ref(text), undo=undo, call=None)
        return card

    def new_card(self, call):
        return {'id': chat.new_id('k'), 'tool': call['tool'], 'label': chat.describe(call), 'at': chat.now_iso(self.clock)}

    def _act(self, job, user, cfg, call):
        """A request the user wrote in so many words: it runs at once (their words are the confirmation)."""
        card = self.execute(user, cfg, call, self.new_card(call))
        job.msg['cards'].append(card)
        job.emit('card', card=card)
        self._say(job, {'done': 'Done.', 'failed': 'That did not work; the card says why.',
                        'refused': 'I can\'t do that for you; the card says why.'}[card['state']])

    def pick_of(self, sub, cid):
        conv = self.store.get(sub, cid)
        return conv.get('model') or self.store.load(sub).get('default_model') or 'auto'

    def _reply(self, job, user, cfg, snap, known):
        """An answer from the first model of the conversation's pick that answers (with tools when it can use them); the
        built-in one from the state when none does. Says why it fell back."""
        conv = self.store.get(job.sub, job.cid)
        history = [m for m in conv['messages'] if m['id'] != job.msg['id']]
        pick = conv.get('model') or self.store.load(job.sub).get('default_model') or 'auto'
        cands, skipped = chat.candidates(cfg, snap, user, self.owner_fn(), self.own_machine_fn(user), pick=pick)
        failed = []
        turns = chat.settings(cfg)['history_turns']
        for cand in cands:
            if job.cancel.is_set():
                return
            native = cand['kind'] == 'ollama'
            messages = chat.model_messages(chat.system_prompt(snap, user, known, native_tools=native), history, turns)
            try:
                if self._converse(job, user, cfg, cand, messages, known):
                    why = cand['before'] + failed
                    if why:
                        self._fallback(job, f'Answered by {cand["label"]} because ' + '; '.join(why) + '.')
                    return
            except (OSError, ValueError):
                failed.append(f'{cand["label"]} did not answer')   # the next model: a machine went to sleep, a tunnel is down
                if job.msg['text']:   # it stopped halfway: the next model starts on a clean answer
                    job.msg['text'] = ''
                    job.emit('replace', text='')
                job.msg['cards'] = [c for c in job.msg['cards'] if c.get('state') != 'proposed']
        text = chat.builtin_answer(history[-1].get('text') or '', snap) if history else None
        self._say(job, text or ('No model of the household is free to answer right now. I can still answer from the dashboard: '
                                'try "What\'s running?", "How much quota is left?" or "Why is the NUC idle?", or queue work '
                                'with "add task in <project>: …".'))
        scope = {'local': 'No local model can answer now', 'online': 'No online agent can answer now'}.get(pick, 'No model can answer now')
        self._fallback(job, f'{scope}: ' + ('; '.join(skipped + failed) or 'none is set up') + '. Pick another model above, or Auto.')

    def _fallback(self, job, text):
        job.msg['fallback'] = text
        job.emit('fallback', text=text)

    def _converse(self, job, user, cfg, cand, messages, known):
        """The model's tool loop: up to ROUNDS rounds; read-only calls run and go back to it; a call that changes something
        becomes a card the user confirms (and ends the loop). False when the model gave nothing at all."""
        msgs = list(messages)
        for rnd in range(ROUNDS):
            if job.msg['text'] and not job.msg['text'].endswith('\n\n'):   # each round's text its own paragraph
                job.msg['text'] += '\n\n'
                job.emit('delta', text='\n\n')
            text, native = self._round(job, cand, cfg, msgs)
            if text is None:
                return rnd > 0
            text, written = chat.split_actions(text)
            calls = native + written
            if not calls or job.cancel.is_set():
                return True
            results, proposed = self._calls(job, user, cfg, calls, known, cand)
            if proposed and not job.msg['text'].strip():
                note = 'Here is what I propose. Nothing happens until you confirm it on the card.'
                job.msg['text'] = note
                job.emit('delta', text=note)
            if proposed or not results:
                return True
            said = [{'function': {'name': c.get('tool'), 'arguments': c.get('args') or {}}} for c in native]
            msgs += [{'role': 'assistant', 'content': text, **({'tool_calls': said} if native else {})}]
            msgs += [{'role': 'tool', 'tool_name': n, 'content': r} if native else
                     {'role': 'user', 'content': f'Tool {n} answered:\n{r}'} for n, r in results]
        if not job.msg['text'].strip():   # every round only looked things up
            job.msg['text'] = NO_ANSWER
            job.emit('replace', text=NO_ANSWER)
        return True

    def _round(self, job, cand, cfg, msgs):
        """One streamed answer: (its text, its native tool calls) or (None, []) when the model gave nothing."""
        got, text, calls = False, '', []
        for piece in self.streams(cand, cfg, msgs, job.cancel):
            if not got:
                job.msg['model'] = cand['label']
                job.emit('model', model=cand['label'])
                got = True
            if isinstance(piece, dict):
                calls += [{'tool': (c.get('function') or {}).get('name'), 'args': (c.get('function') or {}).get('arguments')}
                          for c in piece.get('tool_calls') or []]
                continue
            text += piece
            job.msg['text'] += piece
            job.emit('delta', text=piece)
            if job.cancel.is_set():
                break
        if not got:
            return None, []
        clean, written = chat.split_actions(job.msg['text'])
        if written:   # the ACTION lines were streamed as text: the page shows the answer without them
            job.msg['text'] = clean
            job.emit('replace', text=clean)
        return text, calls

    def _calls(self, job, user, cfg, calls, known, cand):
        """([(tool, result)] of the read-only calls, whether one was proposed). Every call is validated first; a bad one's
        error goes back to the model as its result."""
        results, proposed, tools = [], False, chat.model_tools()
        results += [(c.get('tool') if isinstance(c, dict) else '?', f'skipped: at most {MAX_CALLS} calls at a time; ask again')
                    for c in calls[MAX_CALLS:]]
        for raw in calls[:MAX_CALLS]:
            call = chat.validate(raw, known)
            if isinstance(call, str):
                results.append(((raw or {}).get('tool') if isinstance(raw, dict) else '?', f'refused: {call}'))
                continue
            if not tools[call['tool']]['mutating']:
                _ok, text, _ = run_tool(cfg, call, self.runner)
                results.append((call['tool'], text if len(text) <= 3000 else '(the start is cut)\n' + text[-3000:]))
                continue
            card = self.new_card(call)
            why = chat.refusal(user, call, cfg, self.done_today(user))
            card.update(state='refused', text=why) if why else card.update(
                state='proposed', call=call, text=f'Proposed by {cand["label"]}. Nothing happens until you confirm.')
            job.msg['cards'].append(card)
            job.emit('card', card=card)
            proposed = True
        return results, proposed

    # -- confirming what a model proposed

    def _card_change(self, sub, cid, mid, kid, fn):
        """fn(card) under the store's lock, saved; ChatError when the card is gone."""
        def go(d):
            conv = self.store.conversation(d, cid)
            msg = next((m for m in conv['messages'] if m['id'] == mid), None)
            card = next((k for k in (msg or {}).get('cards') or [] if k['id'] == kid), None)
            if card is None:
                raise chat.ChatError('That card is gone: reload the chat.')
            return fn(card)
        return self.store.change(sub, go)

    def confirm(self, user, cid, mid, kid, yes=True):
        """Runs (or cancels) what a model proposed. The card is marked running under the lock, the tool runs outside it (one
        slow call never blocks other users' chats), then the result is saved."""
        cfg = self.cfg_fn()

        def claim(card):
            if card.get('state') != 'proposed':
                raise chat.ChatError('That proposal was already confirmed or cancelled.')
            card['state'] = 'running' if yes else 'cancelled'
            if not yes:
                card.update(text='Cancelled: nothing was done.', call=None)
            return dict(card)
        card = self._card_change(user['sub'], cid, mid, kid, claim)
        if not yes:
            return card
        call = chat.validate(card['call'], chat.known_names(cfg, self.snap_fn() or {}))   # again: the config may have changed
        if isinstance(call, str):
            card.update(state='failed', text=f'It can no longer run: {call}.', call=None)
        else:
            self.execute(user, cfg, call, card)
        self._card_change(user['sub'], cid, mid, kid, lambda k: k.update(card))
        return card

    # -- Undo

    def undo(self, user, cid, mid, kid):
        cfg = self.cfg_fn()

        def find(d):
            c = self.store.conversation(d, cid)
            m = next((x for x in c['messages'] if x['id'] == mid), None)
            k = next((x for x in (m or {}).get('cards') or [] if x['id'] == kid), None)
            if not k or not k.get('undo') or k.get('state') != 'done':
                raise chat.ChatError('That can no longer be undone (it was undone already, or it never finished).')
            return k
        with self.store.lock:
            card = find(self.store.load(user['sub']))
            back = card['undo']
            target = chat.HOUSEHOLD if back['tool'] == 'control' else user
            try:
                auth.act_for(self.folder, user, target, PREFIX + 'undo ' + card['label'][:80], detail=card['label'], clock=self.clock)
            except auth.AuthError as e:
                raise chat.ChatError('Only an admin can undo that.') from e
            ok, text, _ = run_tool(cfg, back, self.runner)
            if not ok:
                raise chat.ChatError(f'Undo did not work: {plain(text, False)}')

            def mark(d):
                k = find(d)
                k.update(state='undone', undo=None, text=plain(text, True))
            self.store.change(user['sub'], mark)
        return plain(text, True)


def plain(text, ok):
    """A tool's output for the card: its first lines, without a stack trace."""
    lines = [x for x in (text or '').strip().splitlines() if x.strip() and not x.startswith(('Traceback', '  File'))]
    out = '\n'.join(lines[:4])[:400]
    return out or ('Done.' if ok else 'It did not work, and the tool said nothing more. The dashboard\'s log may say why.')


# ---- the HTTP actions (POST /api/chat {"action": ...})


class Stream:
    """A streamed answer: the dashboard writes each event as one JSON line (application/x-ndjson) as it comes."""

    def __init__(self, events):
        self.events = events

    def lines(self):
        yield from ((json.dumps(e) + '\n').encode() for e in self.events)


def post(engine, user, data, snap=None):
    """(status, answer or Stream) of one chat action for `user` ({sub, email, role})."""
    if not isinstance(data, dict):
        return 400, {'error': 'Send a JSON object with an "action".'}
    act = data.get('action')
    if not isinstance(act, str):
        return 400, {'error': 'Send a JSON object with an "action" (a word such as "send").'}
    try:
        return ACTIONS[act](engine, user, data, snap) if act in ACTIONS else \
            (400, {'error': f'Unknown chat action "{act}". Reload the page; it may be older than the dashboard.'})
    except chat.ChatError as e:
        return 409, {'error': str(e)}


def _list(engine, user, data, snap):
    known = chat.known_names(engine.cfg_fn(), snap or {})
    return 200, {'conversations': engine.store.listing(user['sub'], data.get('query') or ''),
                 'suggestions': chat.suggestions(snap or {}, user, known, engine.cfg_fn()), 'role': user['role'],
                 'limits': {**chat.settings(engine.cfg_fn()), 'attachment_bytes': chat.MAX_ATTACHMENT_BYTES,
                            'attachments': chat.MAX_ATTACHMENTS, 'text': chat.MAX_TEXT},
                 'host': (snap or {}).get('where', {}).get('machine') or (snap or {}).get('where', {}).get('hostname'),
                'models': chat.choices(engine.cfg_fn(), snap or {}, user, engine.owner_fn(), engine.own_machine_fn(user)),
                'default_model': engine.store.load(user['sub']).get('default_model') or 'auto'}


def _get(engine, user, data, _snap):
    return 200, {'conversation': engine.conversation(user, str(data.get('id') or ''))}


def _send(engine, user, data, _snap):
    model = data.get('model') if chat.valid_pick(str(data.get('model') or ''), engine.cfg_fn()) else None
    job = engine.send(user, data.get('conversation') or None, data.get('text'), data.get('attachments'), edit=data.get('edit'),
                      retry=data.get('retry'), model=model)
    return 200, Stream(job.follow())


def _follow(engine, user, data, _snap):
    with engine.lock:
        job = engine.jobs.get(str(data.get('message') or ''))
    if job is None or job.sub != user['sub']:
        return 404, {'error': 'That answer has finished; reload the chat to see it.'}
    return 200, Stream(job.follow())


def _stop(engine, user, data, _snap):
    return 200, {'ok': True, 'stopped': engine.stop(user, str(data.get('message') or ''))}


def _new(engine, user, data, _snap):
    return 200, {'conversation': engine.store.create(user['sub'], data.get('title') or '')}


def _rename(engine, user, data, _snap):
    engine.store.rename(user['sub'], str(data.get('id') or ''), data.get('title'))
    return 200, {'ok': True}


def _delete(engine, user, data, _snap):
    engine.store.delete(user['sub'], str(data.get('id') or ''))
    return 200, {'ok': True}


def _clear(engine, user, _data, _snap):
    engine.store.clear(user['sub'])
    return 200, {'ok': True}


def _undo(engine, user, data, _snap):
    text = engine.undo(user, str(data.get('conversation') or ''), str(data.get('message') or ''), str(data.get('card') or ''))
    return 200, {'ok': True, 'text': text, 'conversation': engine.store.get(user['sub'], str(data.get('conversation')))}


def _confirm(engine, user, data, _snap):
    card = engine.confirm(user, str(data.get('conversation') or ''), str(data.get('message') or ''), str(data.get('card') or ''),
                          yes=data.get('yes', True) is not False)
    return 200, {'ok': True, 'card': card}


def _set_model(engine, user, data, _snap):
    """The model picker: the conversation's choice (when one is open) and the user's default for new chats."""
    pick = str(data.get('model') or '')
    if not chat.valid_pick(pick, engine.cfg_fn()):
        return 400, {'error': f'There is no model "{pick}" here. Reload the page to see the current list.'}
    cid = data.get('id')

    def go(d):
        d['default_model'] = pick
        if cid:
            engine.store.conversation(d, cid)['model'] = pick
    engine.store.change(user['sub'], go)
    return 200, {'ok': True, 'model': pick}


def _activity(engine, user, _data, _snap):
    if user.get('role') != 'admin':
        return 403, {'error': 'Household activity is for admins. Your own chats are in the list on the left.'}
    return 200, {'activity': engine.activity()}


ACTIONS = {'list': _list, 'get': _get, 'send': _send, 'follow': _follow, 'stop': _stop, 'new': _new, 'rename': _rename,
           'delete': _delete, 'clear': _clear, 'undo': _undo, 'activity': _activity,
           'confirm': _confirm, 'model': _set_model}


# ---- the dashboard's glue


API = '/api/chat'
BODY_LIMIT = 320 * 1024   # three 32 KB attachments and a long message, JSON-escaped
LOCAL_USER = {'sub': 'local', 'email': None, 'role': 'admin'}   # sign-in off: the dashboard serves its owner on this computer


def user_of(collector, headers):
    """Who is asking: the signed-in user, or the owner on this computer when sign-in is off; None when nobody is signed in."""
    if not getattr(collector, 'signed', False):
        return dict(LOCAL_USER)
    who = getattr(collector, 'who', None)
    return who(headers) if callable(who) else None


def _auth_runtime(collector):
    return getattr(getattr(collector, 'httpd', None), 'auth', None)


def owner_of(collector):
    """The first admin who signed in: the owner of this host's config and its accounts."""
    rt = _auth_runtime(collector)
    if rt is None:
        return None
    admins = [(u.get('created') or 0, sub) for sub, u in rt.users.all().items() if u.get('role') == 'admin']
    return min(admins)[1] if admins else None


def own_machine(collector, user):
    """The asking user's own computer (its record's main_machine, when the household knows it), never needed by the chat."""
    rt = _auth_runtime(collector)
    rec = rt.users.get(user.get('sub')) if rt is not None else None
    return (rec or {}).get('main_machine')


def engine_of(collector):
    eng = collector.__dict__.get('chat_engine')
    if eng is None:
        eng = Engine(lambda: collector.cfg, collector.snapshot, owner_fn=lambda: owner_of(collector),
                     own_machine_fn=lambda u: own_machine(collector, u))
        collector.chat_engine = eng
    return eng


def handle(collector, headers, raw):
    """(status, answer or Stream) of POST /api/chat."""
    user = user_of(collector, headers)
    if user is None:
        return 401, {'error': 'You are signed out. Reload the page and sign in again.'}
    try:
        data = json.loads(raw or b'null')
    except ValueError:
        data = None
    eng = engine_of(collector)
    snap = collector.snapshot() if isinstance(data, dict) and data.get('action') == 'list' else None
    return post(eng, user, data, snap)
