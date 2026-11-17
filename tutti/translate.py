"""Translation with mandatory QA, on local models only. No string reaches <locale>.json before it has passed
  1. the validators (placeholders, kept names, end punctuation, untranslated text, length), and
  2. the back-translation check: a second model translates it back and judges whether the meaning survived.
A failing string is re-translated with the reasons; what still fails is held for a human (held-<locale>.jsonl) and left out.
If a model host is down the run waits; it never skips QA. Each output gets <locale>.qa.json (policy, SHA-256, a verdict per
string); `check` refuses an output without a matching passing report, so an import can refuse it too.
Catalogues are flat JSON objects: {"key": "text with {placeholders}"}."""
import hashlib
import json
import os
import re
import time

from . import local

POLICY = 'validators+back-translation/v1'
PLACEHOLDER = re.compile(r'\{\s*([A-Za-z0-9_]+)')
ENDS = {'.': '.。।۔', '?': '?？؟;', '!': '!！', ':': ':：', '…': '…'}


def end_class(s):
    s = s.rstrip()
    if s.endswith('...'):
        return '…'
    return next((k for k, chars in ENDS.items() if s and s[-1] in chars), '')


def has_words(s, keep=()):
    for name in keep:
        s = s.replace(name, ' ')
    return bool(re.search(r'[^\W\d_]{2,}', PLACEHOLDER.sub(' ', s)))


def validate(source, text, keep=()):
    """The mechanical checks. Returns the reasons a translation fails; an empty list passes."""
    errs = []
    if not text.strip():
        return ['the translation is empty']
    if sorted(PLACEHOLDER.findall(source)) != sorted(PLACEHOLDER.findall(text)):
        errs.append(f'placeholders differ: keep exactly {sorted(set(PLACEHOLDER.findall(source)))} unchanged')
    errs += [f'the name "{name}" must stay as it is' for name in keep if name in source and name not in text]
    if end_class(source) != end_class(text):
        errs.append(f'end punctuation differs: the source ends with {end_class(source) or "no punctuation"!r}')
    if text.strip() == source.strip() and has_words(source, keep):
        errs.append('the text was left untranslated')
    if len(text) > 3 * len(source) + 12:
        errs.append(f'too long: {len(text)} characters for a source of {len(source)}')
    return errs


def verdict(answer):
    """The judge answers OK, MINOR or WRONG first; anything unreadable counts as WRONG (QA never passes by accident)."""
    m = re.match(r'\W*(OK|MINOR|WRONG)\b', answer.strip().upper())
    return m.group(1) if m else 'WRONG'


def sha256(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def load(path, default=None):
    try:
        with open(path, encoding='utf8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, obj):
    with open(path + '.tmp', 'w', encoding='utf8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(path + '.tmp', path)


def _stage(role, prompt):
    if role == 'translator':
        return 'translate'
    return 'judge2' if prompt.lstrip().startswith('Original:') else 'back-translate'


def _task(stage):
    names = {'translate': 'ltrans translate stage', 'back-translate': 'ltrans back-translation stage',
             'judge2': 'ltrans second judge stage'}
    return {'title': names[stage], 'scope': ['locale.json'], 'size': 'S'}


def _machine_rows(cfg, names, probe_fn):
    from . import fleet, state
    rows = {}
    for name in names:
        machine = fleet.machine_of(cfg, name)
        spec = (cfg.get('machines') or {}).get(machine)
        if machine in rows or not spec:
            continue
        rows[machine] = state.machine_state(spec, probe_fn(spec), None)
    return rows


def _pick_local_worker(cfg, names, task, rows):
    from . import fleet
    choices = []
    machines = list(rows.values())
    for name in names:
        worker = cfg['workers'].get(name) or {}
        if worker.get('kind') != 'ollama' or worker.get('enabled') is False:
            continue
        machine = fleet.machine_of(cfg, name)
        row = rows.get(machine) or {}
        blocked = fleet.governor(cfg, name, machines, mem_gb=row.get('mem_free_gb'), hardware=worker.get('hardware') or {})
        if blocked:
            continue
        why, model = fleet.fits(worker, task)
        if why or not model:
            continue
        free = row.get('status') == 'free'
        choices.append((not free, -(model.get('params_b') or 0.0), fleet.watts_of(cfg, machine), name, model['name']))
    if not choices:
        return None, None
    _, _, _, worker_name, model_name = min(choices)
    return worker_name, model_name


def _picker(cfg, t, probe_ttl=15, clock=time.time, probe_fn=None):
    from . import fleet, remote
    local_names = fleet.local_names(cfg)
    fallback = {r: t.get(r, '') for r in ('translator', 'judge')}
    cache = {'at': 0.0, 'rows': {}}
    probe = probe_fn or remote.probe

    def rows():
        if not cache['rows'] or clock() - cache['at'] >= probe_ttl:
            cache['rows'] = _machine_rows(cfg, local_names, probe)
            cache['at'] = clock()
        return cache['rows']

    def generate(role, prompt):
        worker_name, model_name = _pick_local_worker(cfg, local_names, _task(_stage(role, prompt)), rows())
        chosen = worker_name
        worker = cfg['workers'].get(chosen)
        if not worker or worker.get('kind') != 'ollama':
            raise RuntimeError(f'translate.{role} is not a usable local model worker: choose an ollama worker in the config')
        active = {**worker, '_name': chosen}
        if model_name:
            active['model'] = model_name
        return local.generate(cfg, active, prompt, wait=True, low_priority=t.get('low_priority', False))
    return generate


def translate_one(gen, key, source, language, src_language, keep, feedback=''):
    names = f' Never translate these names: {", ".join(keep)}.' if keep else ''
    return gen('translator', f'Translate this user-interface text from {src_language} to {language}. Keep every {{placeholder}} '
               f'exactly.{names} Keep the end punctuation. Key: {key}.{feedback}\nAnswer with the translation only.\n\n{source}')


def back_check(gen, source, text, language, src_language):
    back = gen('judge', f'Translate this {language} text to {src_language}. Answer with the translation only.\n\n{text}')
    answer = gen('judge', f'Original: {source}\nBack-translation of its {language} translation: {back}\n'
                 'Does the translation add, omit or change any information? Answer with one word first: OK (same information), '
                 'MINOR (only wording differs) or WRONG (information added, omitted or changed), then a short reason.')
    return verdict(answer), answer.strip()


def qa_string(gen, key, source, language, src_language, keep, retries=2):
    """Translate, validate, back-check; retry with the reasons. Returns (text or None, record for the report)."""
    feedback, reasons = '', []
    for attempt in range(retries + 1):
        text = translate_one(gen, key, source, language, src_language, keep, feedback)
        reasons = validate(source, text, keep)
        if not reasons:
            v, why = back_check(gen, source, text, language, src_language)
            if v != 'WRONG':
                return text, {'status': 'pass', 'verdict': v, 'attempts': attempt + 1}
            reasons = [f'meaning check: {why}']
        feedback = ' A previous attempt failed: ' + '; '.join(reasons) + '.'
    return None, {'status': 'held', 'reasons': reasons, 'attempts': retries + 1}


def run(cfg, source_path, locale, out_dir, gen=None):
    """Translates and QAs one catalogue into out_dir. Incremental: a string whose source text already passed is kept."""
    t = cfg.get('translate') or {}
    gen = gen or _picker(cfg, t)
    language, src_language, keep = (t.get('languages') or {}).get(locale, locale), t.get('source_language', 'English'), t.get('keep', [])
    os.makedirs(out_dir, exist_ok=True)
    out, report_path = os.path.join(out_dir, f'{locale}.json'), os.path.join(out_dir, f'{locale}.qa.json')
    source, done, old = load(source_path), load(out, {}), load(report_path, {})
    fresh = old.get('policy') == POLICY
    strings, result, held = {}, {}, []
    for key, text in source.items():
        digest = hashlib.sha256(text.encode()).hexdigest()
        prev = old.get('strings', {}).get(key, {})
        if fresh and prev.get('status') == 'pass' and prev.get('source') == digest and key in done:
            result[key], strings[key] = done[key], prev
            continue
        translated, record = qa_string(gen, key, text, language, src_language, keep, t.get('retries', 2))
        strings[key] = {**record, 'source': digest}
        if translated is None:
            held.append({'key': key, 'source': text, **record})
        else:
            result[key] = translated
    save(out, result)
    with open(os.path.join(out_dir, f'held-{locale}.jsonl'), 'w', encoding='utf8') as f:
        f.writelines(json.dumps(h, ensure_ascii=False) + '\n' for h in held)
    save(report_path, {'policy': POLICY, 'status': 'pass', 'sha256': sha256(out), 'passed': len(result), 'held': len(held),
                       'strings': strings})
    return len(result), len(held)


def check(out_path):
    """Why an output must not be imported, or '' when its QA report matches it."""
    report = load(re.sub(r'\.json$', '.qa.json', out_path))
    if not report:
        return f'{out_path} has no QA report beside it: run `tutti translate` to produce it (translations are never imported un-QA\'d)'
    if report.get('policy') != POLICY or report.get('status') != 'pass':
        return f'the QA report of {out_path} is from another policy or did not pass: run `tutti translate` again'
    if report.get('sha256') != sha256(out_path):
        return f'{out_path} changed after its QA: run `tutti translate` again so the edited strings are checked'
    passed = {k for k, s in report['strings'].items() if s.get('status') == 'pass'}
    if set(load(out_path, {})) != passed:
        return f'{out_path} holds keys that did not pass QA: run `tutti translate` again'
    return ''
