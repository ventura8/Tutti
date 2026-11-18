'use strict';

function fillSchedule(s) {
    const MY_DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
    const days = document.getElementById('my-days');
    if (!days) return;
    days.replaceChildren(el('legend', { class: 'row__note', text: 'Working days' }), ...MY_DAYS.map((d) => el('label', {},
        el('input', { type: 'checkbox', value: d, checked: s.days.includes(d) }), ` ${d}`)));
    document.getElementById('my-window').value = s.window;
    document.getElementById('my-hours').value = s.hours;
    const vacation = String(s.mode).startsWith('vacation-until-');
    document.getElementById('my-mode').value = vacation ? 'vacation' : s.mode;
    document.getElementById('my-until').value = vacation ? s.mode.slice('vacation-until-'.length) : '';
    document.getElementById('my-until-label').hidden = !vacation;
}

async function startSchedule() {
    const res = await fetch('/api/me/schedule', { cache: 'no-store' }).catch(() => null);
    if (!res?.ok) return;
    fillSchedule((await res.json()).schedule);
    document.getElementById('my-schedule').hidden = false;
    document.getElementById('my-mode').addEventListener('change', () => { 
        document.getElementById('my-until-label').hidden = document.getElementById('my-mode').value !== 'vacation'; 
    });
    document.getElementById('my-schedule-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const days = [...document.getElementById('my-days').querySelectorAll('input:checked')].map((i) => i.value);
        const mode = document.getElementById('my-mode').value === 'vacation' ? `vacation-until-${document.getElementById('my-until').value}` : document.getElementById('my-mode').value;
        const res = await fetch('/api/me/schedule', {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': document.querySelector('meta[name="tutti-token"]')?.content }, 
            body: JSON.stringify({ days, window: document.getElementById('my-window').value, hours: Number(document.getElementById('my-hours').value), mode }),
        });
        const answer = await res.json().catch(() => ({}));
        if (!res.ok) { showBanner(el('strong', { text: 'Your schedule was not saved.' }), answer.error); return; }
        fillSchedule(answer.schedule); document.getElementById('my-schedule-note').textContent = 'Saved.';
    });
}

function showScheduleView() {
    const on = location.hash.startsWith('#schedule');
    const scheduleView = document.getElementById('schedule-view');
    if (!scheduleView) return;
    scheduleView.hidden = !on;
    if (on) {
        document.getElementById('dashboard-view').hidden = true;
        document.getElementById('settings-view').hidden = true;
        document.getElementById('users-view').hidden = true;
        document.getElementById('chat-view').hidden = true;
    }
    for (const a of document.querySelectorAll('.tabs__tab[data-view]')) {
        const current = on ? a.dataset.view === 'schedule' : a.dataset.view !== 'schedule' && a.getAttribute('aria-current') === 'page';
        if (current) a.setAttribute('aria-current', 'page');
        else a.removeAttribute('aria-current');
    }
}

document.addEventListener('DOMContentLoaded', () => {
    window.addEventListener('hashchange', showScheduleView);
    showScheduleView();
    void startSchedule();
});
