// Tutti dashboard: keyboard and screen-reader behaviour shared by every tab (tests/e2e/test_ui.py checks it in a browser).
// - A form field the browser refuses (too small, missing, wrong pattern) is marked aria-invalid and gets an error message
//   with role="alert" tied to it by aria-describedby, so a screen reader says what is wrong; it clears as the value is fixed.
// - Esc closes the open disclosure (<details>) that holds the focus and gives the focus back to its summary.
'use strict';

function tuttiFieldError(field) {
    const id = `${field.id || field.name || 'field'}-error`;
    let msg = document.getElementById(id);
    if (!msg) {
        msg = document.createElement('p');
        msg.id = id;
        msg.className = 'field-error';
        msg.setAttribute('role', 'alert');
        (field.closest('label, .row__stats') || field).after(msg);
        const ids = (field.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
        if (!ids.includes(id)) field.setAttribute('aria-describedby', [...ids, id].join(' '));
    }
    return msg;
}

function tuttiShowFieldError(field) {
    const v = field.validity;
    let why = field.validationMessage;
    if (v.rangeUnderflow || v.rangeOverflow) why = `Enter a number from ${field.min} to ${field.max}.`;
    else if (v.valueMissing) why = 'This field is needed: fill it in, then save.';
    else if (v.patternMismatch && field.title) why = field.title;
    field.setAttribute('aria-invalid', 'true');
    tuttiFieldError(field).textContent = why;
}

function tuttiClearFieldError(field) {
    if (field.getAttribute('aria-invalid') !== 'true' || !field.checkValidity()) return;
    field.removeAttribute('aria-invalid');
    const msg = document.getElementById(`${field.id || field.name || 'field'}-error`);
    if (msg) msg.textContent = '';
}

function tuttiEscapeDetails(e) {
    if (e.key !== 'Escape') return;
    const open = document.activeElement?.closest?.('details[open]');
    if (!open) return;
    open.open = false;
    open.querySelector(':scope > summary')?.focus();
}

if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('invalid', (e) => tuttiShowFieldError(e.target), true);
    document.addEventListener('input', (e) => { if (e.target.matches?.('input, select, textarea')) tuttiClearFieldError(e.target); }, true);
    document.addEventListener('keydown', tuttiEscapeDetails);
}

if (typeof module !== 'undefined') module.exports = { tuttiFieldError, tuttiShowFieldError, tuttiClearFieldError, tuttiEscapeDetails };
