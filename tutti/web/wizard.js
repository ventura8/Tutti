// wizard.js: The setup wizard.

const STEPS = [
    { 
        title: 'Welcome', 
        render: () => el('p', { text: 'Welcome to the Tutti setup wizard! This guide will walk you through configuring your household, machines, and integrations in just a few steps.' }) 
    },
    { 
        title: 'Choose Machines', 
        render: () => el('div', { class: 'form-group' },
            el('label', { text: 'Main Tutti Machine (Required):' }),
            el('input', { type: 'text', placeholder: 'hostname-main' }),
            el('p', { class: 'note', text: 'The machine where your main household processes run.' }),
            el('label', { text: 'Secondary (Failover) Machine:' }),
            el('input', { type: 'text', placeholder: 'hostname-failover' }),
            el('p', { class: 'note', text: 'Optional machine for backup and failover.' })
        ) 
    },
    { 
        title: 'Sign in Accounts', 
        render: () => el('div', { class: 'form-group' },
            el('p', { text: 'Click each button to sign in. Follow the instructions in the new tab for each service.' }),
            el('button', { class: 'btn btn--tonal', text: 'Sign in to Claude' }),
            el('button', { class: 'btn btn--tonal', text: 'Sign in to Cursor' }),
            el('button', { class: 'btn btn--tonal', text: 'Sign in to GitHub' }),
            el('button', { class: 'btn btn--tonal', text: 'Sign in to agy' }),
            el('button', { class: 'btn btn--tonal', text: 'Sign in to Copilot' }),
            el('button', { class: 'btn btn--tonal', text: 'Sign in to local models' })
        ) 
    },
    { 
        title: 'Connect Integrations', 
        render: () => el('div', { class: 'form-group' },
            el('label', { text: 'Add Devices (Pairing):' }),
            el('input', { type: 'text', placeholder: 'Enter device name/ID' }),
            el('label', { text: 'Add Repositories (GitHub URLs):' }),
            el('input', { type: 'text', placeholder: 'https://github.com/user/repo' }),
            el('label', { text: 'Household Integrations:' }),
            el('input', { type: 'text', placeholder: 'e.g., smart-home, calendar' })
        ) 
    },
    { 
        title: 'Finish', 
        render: () => el('p', { text: 'You are all set! Click Finish to apply your changes and start using Tutti.' }) 
    }
];

let currentStep = 0;

function renderWizard() {
    const content = $('wizard-content');
    if (!content) return;
    
    const step = STEPS[currentStep];
    const stepDiv = el('div', { class: 'wizard-step' },
        el('h3', { text: `${currentStep + 1}. ${step.title}` }),
        step.render(),
        el('div', { class: 'wizard-nav' },
            currentStep > 0 ? el('button', { class: 'btn btn--tonal', text: 'Back', onclick: () => navigate(-1) }) : null,
            currentStep < STEPS.length - 1 ? el('button', { class: 'btn btn--filled', text: 'Next', onclick: () => navigate(1) }) : el('button', { class: 'btn btn--filled', text: 'Finish', onclick: () => finishWizard() })
        )
    );
    content.replaceChildren(stepDiv);
}

function navigate(dir) {
    currentStep += dir;
    renderWizard();
}

function finishWizard() {
    // Basic finish action; backend integration should be added here later
    alert('Setup applied! Redirecting to dashboard...');
    window.location.hash = '#dashboard';
}

// Ensure wizard is rendered when needed
window.addEventListener('load', () => {
    // Check if wizard should be shown
    if (window.location.hash === '#wizard') {
        showWizard();
    }
});

window.addEventListener('hashchange', () => {
    if (window.location.hash === '#wizard') {
        showWizard();
    } else {
        // Hide wizard if navigating away
        const wizardView = $('wizard-view');
        if (wizardView) wizardView.hidden = true;
    }
});

function showWizard() {
    // Hide other views
    ['chat-view', 'dashboard-view', 'users-view', 'settings-view'].forEach(id => {
        const el = $(id);
        if (el) el.hidden = true;
    });
    // Show wizard
    const wizardView = $('wizard-view');
    if (wizardView) wizardView.hidden = false;
    renderWizard();
}

