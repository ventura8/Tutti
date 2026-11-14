// Tutti dashboard: Report a problem (docs/REPORT.md). The person writes what happened and what they expected, previews the
// whole report (the server adds the scrubbed diagnostics), then sends it: a GitHub issue when gh is signed in on the
// dashboard's computer, else GitHub's "new issue" page opens with the report filled in. Also the first-run consent banner.
'use strict';

const REPORT_TOKEN = document.querySelector('meta[name="tutti-token"]')?.content || '';
let reportPreviewId = '';

async function reportPost(send) {
    const res = await fetch('/api/report', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Tutti-Token': REPORT_TOKEN },
        body: JSON.stringify({ what: $('report-what').value, expected: $('report-expected').value, send, preview: reportPreviewId }),
    });
    const answer = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(answer.error || `the dashboard answered ${res.status}: try again, or run tutti report in a terminal`);
    return answer;
}

function reportStatus(text) {
    $('report-status').textContent = text;
}

async function reportPreview() {
    reportStatus('Preparing the preview…');
    try {
        const p = await reportPost(false);
        reportPreviewId = p.preview;
        $('report-preview').textContent = `${p.title}\n\n${p.body}`;
        $('report-preview').hidden = false;
        $('report-send').disabled = false;
        reportStatus('This is everything that will be sent. Change the text above and preview again, or Send.');
    } catch (err) {
        reportStatus(err.message);
    }
}

async function reportSend() {
    $('report-send').disabled = true;
    reportStatus('Sending…');
    try {
        const done = await reportPost(true);
        const extra = done.sentry ? ' It also went to the crash reports, with the last crash.' : '';
        if (done.how === 'issue') {
            reportStatus(`Sent. Thank you. You can follow it at ${done.url}.${extra}`);
        } else {
            window.open(done.url, '_blank', 'noopener,noreferrer');
            reportStatus('GitHub opened in a new tab with your report filled in: choose "Submit new issue" there to send it. '
                + `If no tab opened, allow pop-ups for this page and press Send again.${extra}`);
            $('report-send').disabled = false;
        }
    } catch (err) {
        reportStatus(err.message);
        $('report-send').disabled = false;
    }
}

function renderConsent(s) {
    const st = s.telemetry?.status;
    $('consent-banner').hidden = !st || st.asked;
}

document.addEventListener('DOMContentLoaded', () => {
    $('report-open').addEventListener('click', () => {
        $('report-preview').hidden = true;
        $('report-send').disabled = true;
        reportStatus('');
        $('report-dialog').showModal();
    });
    $('report-cancel').addEventListener('click', () => $('report-dialog').close());
    $('report-what').addEventListener('input', () => { $('report-send').disabled = true; });
    $('report-expected').addEventListener('input', () => { $('report-send').disabled = true; });
    $('report-preview-btn').addEventListener('click', () => void reportPreview());
    $('report-send').addEventListener('click', () => void reportSend());
    $('consent-yes').addEventListener('click', () => void telemetrySwitch('/api/telemetry', { consent: true }, 'Your answer was not saved.'));
    $('consent-no').addEventListener('click', () => void telemetrySwitch('/api/telemetry', { consent: false }, 'Your answer was not saved.'));
});
