// Tutti dashboard: the Tasks tab. Displays tasks filtered by status: Active, Queued, Stalled, Needs a person, Backlog.
'use strict';

function switchTaskFilter(filter) {
    // Hide all task sections
    document.querySelectorAll('#tasks-view [id^="tasks-"]').forEach(el => el.hidden = true);
    
    // Show the selected one
    const target = document.getElementById(`tasks-${filter}`);
    if (target) target.hidden = false;
    
    // Update tabs
    document.querySelectorAll('#tasks-view .tabs__tab').forEach(tab => {
        if (tab.dataset.taskFilter === filter) tab.setAttribute('aria-current', 'page');
        else tab.removeAttribute('aria-current');
    });
}

function showTasksView() {
    const on = location.hash.startsWith('#tasks');
    document.getElementById('tasks-view').hidden = !on;
    if (on) {
        document.getElementById('dashboard-view').hidden = true;
        document.getElementById('settings-view').hidden = true;
        document.getElementById('chat-view').hidden = true;
        document.getElementById('users-view').hidden = true;
    }
    for (const a of document.querySelectorAll('.tabs__tab[data-view]')) {
        const current = on ? a.dataset.view === 'tasks' : a.dataset.view !== 'tasks' && a.getAttribute('aria-current') === 'page';
        if (current) a.setAttribute('aria-current', 'page');
        else a.removeAttribute('aria-current');
    }
}

document.addEventListener('DOMContentLoaded', () => {
    window.addEventListener('hashchange', showTasksView);
    showTasksView();

    document.querySelectorAll('#tasks-view .tabs__tab').forEach(tab => {
        tab.addEventListener('click', () => switchTaskFilter(tab.dataset.taskFilter));
    });
    
    // Default filter
    const hashFilter = location.hash.split('/')[1];
    switchTaskFilter(hashFilter && ['active', 'queued', 'stalled', 'attention', 'backlog'].includes(hashFilter) ? hashFilter : 'active');
});
