"""The dashboard's accessibility behaviour (tutti/web/a11y.js, run in node): error messages with role="alert"
tied to refused form fields by aria-describedby, clearing on valid input, and escaping open disclosures."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
A11Y = ROOT / 'tutti' / 'web' / 'a11y.js'

HARNESS = """
const elements = new Map();

class Element {
    constructor(tag, id = '') {
        this.tagName = tag.toUpperCase();
        this._id = id;
        this.className = '';
        this.textContent = '';
        this.attrs = {};
        this.validity = {};
        this.validationMessage = '';
        this.valid = true;
        this.min = '';
        this.max = '';
        this.title = '';
        this.name = '';
        this.open = false;
        this.focused = false;
        this._parent = null;
        this._afterNode = null;
        this._kids = [];
        if (id) elements.set(id, this);
    }
    get id() { return this._id; }
    set id(val) {
        if (this._id) elements.delete(this._id);
        this._id = val;
        if (val) elements.set(val, this);
    }
    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
    }
    setAttribute(name, value) {
        this.attrs[name] = String(value);
    }
    removeAttribute(name) {
        delete this.attrs[name];
    }
    checkValidity() {
        return Boolean(this.valid);
    }
    closest(selector) {
        if (selector === 'details[open]') {
            if (this.tagName === 'DETAILS' && this.open) return this;
            return this._parent ? this._parent.closest(selector) : null;
        }
        const tags = selector.split(',').map((s) => s.trim().toUpperCase());
        if (tags.includes(this.tagName)) return this;
        return this._parent ? this._parent.closest(selector) : null;
    }
    after(node) {
        this._afterNode = node;
    }
    querySelector(selector) {
        if (selector === ':scope > summary' || selector === 'summary') {
            return this._kids.find((k) => k.tagName === 'SUMMARY') || null;
        }
        return null;
    }
    focus() {
        this.focused = true;
    }
    append(...kids) {
        for (const k of kids) {
            k._parent = this;
            this._kids.push(k);
        }
    }
}

const listeners = {};
global.document = {
    createElement: (tag) => new Element(tag),
    getElementById: (id) => elements.get(id) || null,
    addEventListener: (type, handler) => { listeners[type] = handler; },
    activeElement: null,
};
"""


def run_node(body):
    script = f'const A11Y_PATH = {json.dumps(str(A11Y))};\n{HARNESS}\nconst a11y = require(A11Y_PATH);\n{body}'
    proc = subprocess.run(['node', '-'], input=script, capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


@unittest.skipUnless(shutil.which('node'), 'node is not installed: the a11y tests run where node is (CI has it)')
class A11yTest(unittest.TestCase):
    def test_bad_case_invalid_field_gets_aria_invalid_and_alert_role(self):
        # A form field the browser refuses is marked aria-invalid, gets a role="alert" message tied to it
        res = run_node("""
const field = new Element('input', 'pct');
field.min = '10';
field.max = '90';
field.validity = { rangeUnderflow: true };
field.valid = false;
listeners.invalid({ target: field });
const msg = document.getElementById('pct-error');
console.log(JSON.stringify({
    invalid: field.getAttribute('aria-invalid'),
    describedby: field.getAttribute('aria-describedby'),
    msgRole: msg ? msg.getAttribute('role') : null,
    msgClass: msg ? msg.className : null,
    text: msg ? msg.textContent : null,
}));
""")
        self.assertEqual(res['invalid'], 'true')
        self.assertEqual(res['describedby'], 'pct-error')
        self.assertEqual(res['msgRole'], 'alert')
        self.assertEqual(res['msgClass'], 'field-error')
        self.assertEqual(res['text'], 'Enter a number from 10 to 90.')

    def test_good_case_valid_field_clears_error(self):
        # When a field becomes valid, aria-invalid is removed and the error text is emptied
        res = run_node("""
const field = new Element('input', 'pct');
field.validity = { valueMissing: true };
field.valid = false;
a11y.tuttiShowFieldError(field);
const msg = document.getElementById('pct-error');
const before = { invalid: field.getAttribute('aria-invalid'), text: msg.textContent };

field.validity = {};
field.valid = true;
field.matches = () => true;
listeners.input({ target: field });
const after = { invalid: field.getAttribute('aria-invalid'), text: msg.textContent };
console.log(JSON.stringify({ before, after }));
""")
        self.assertEqual(res['before'], {'invalid': 'true', 'text': 'This field is needed: fill it in, then save.'})
        self.assertIsNone(res['after']['invalid'])
        self.assertEqual(res['after']['text'], '')

    def test_bad_case_field_still_invalid_does_not_clear_error(self):
        # If the field is still invalid, tuttiClearFieldError does not clear the error message or attribute
        res = run_node("""
const field = new Element('input', 'pct');
field.validity = { valueMissing: true };
field.valid = false;
a11y.tuttiShowFieldError(field);
a11y.tuttiClearFieldError(field);
const msg = document.getElementById('pct-error');
console.log(JSON.stringify({
    invalid: field.getAttribute('aria-invalid'),
    text: msg.textContent,
}));
""")
        self.assertEqual(res['invalid'], 'true')
        self.assertEqual(res['text'], 'This field is needed: fill it in, then save.')

    def test_validation_message_variants(self):
        # Covers patternMismatch with title, and default validationMessage fallback
        res = run_node("""
const f1 = new Element('input', 'code');
f1.validity = { patternMismatch: true };
f1.title = 'Must be uppercase';
a11y.tuttiShowFieldError(f1);

const f2 = new Element('input', 'email');
f2.validity = { typeMismatch: true };
f2.validationMessage = 'Please enter an email';
a11y.tuttiShowFieldError(f2);

console.log(JSON.stringify({
    codeMsg: document.getElementById('code-error').textContent,
    emailMsg: document.getElementById('email-error').textContent,
}));
""")
        self.assertEqual(res['codeMsg'], 'Must be uppercase')
        self.assertEqual(res['emailMsg'], 'Please enter an email')

    def test_error_id_fallbacks_and_preserved_describedby(self):
        # Uses field.name or 'field' fallback, and appends to existing aria-describedby without duplicate
        res = run_node("""
const fNamed = new Element('input');
fNamed.name = 'account';
fNamed.setAttribute('aria-describedby', 'existing-hint');
a11y.tuttiFieldError(fNamed);
const firstDescribed = fNamed.getAttribute('aria-describedby');

a11y.tuttiFieldError(fNamed);
const secondDescribed = fNamed.getAttribute('aria-describedby');

const fAnon = new Element('input');
const anonMsg = a11y.tuttiFieldError(fAnon);

console.log(JSON.stringify({
    namedId: document.getElementById('account-error') ? 'account-error' : null,
    firstDescribed,
    secondDescribed,
    anonId: anonMsg.id,
}));
""")
        self.assertEqual(res['namedId'], 'account-error')
        self.assertEqual(res['firstDescribed'], 'existing-hint account-error')
        self.assertEqual(res['secondDescribed'], 'existing-hint account-error')
        self.assertEqual(res['anonId'], 'field-error')

    def test_escape_closes_open_disclosure_and_focuses_summary(self):
        # Escape key closes an open <details> holding activeElement and focuses its summary
        res = run_node("""
const details = new Element('details', 'disclosure');
details.open = true;
const summary = new Element('summary');
const btn = new Element('button');
details.append(summary, btn);

document.activeElement = btn;
listeners.keydown({ key: 'Escape' });
const closed = { open: details.open, summaryFocused: summary.focused };

details.open = true;
summary.focused = false;
a11y.tuttiEscapeDetails({ key: 'Enter' });
const nonEscape = { open: details.open, summaryFocused: summary.focused };

console.log(JSON.stringify({ closed, nonEscape }));
""")
        self.assertEqual(res['closed'], {'open': False, 'summaryFocused': True})
        self.assertEqual(res['nonEscape'], {'open': True, 'summaryFocused': False})


if __name__ == '__main__':
    unittest.main()
