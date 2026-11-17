// Usage chart interaction. English is the dashboard's only language catalogue.
const sparkText = {
    help: 'Usage history. Use arrow keys to select readings.',
    reset: 'from reset',
    used: 'used',
    pace: 'pace line',
    points: 'points',
};

function sparkReadout(a, r) {
    const at = Date.parse(r.at);
    const elapsed = Math.round((at - Date.parse(a.reset)) / 1000);
    const sign = elapsed < 0 ? '−' : '+';
    const seconds = Math.abs(elapsed);
    const hours = Math.floor(seconds / 3600);
    const minutes = String(Math.floor(seconds % 3600 / 60)).padStart(2, '0');
    const secs = String(seconds % 60).padStart(2, '0');
    const pace = Math.max(0, Math.min(100, (at - Date.parse(a.window_start))
        / (Date.parse(a.target) - Date.parse(a.window_start)) * 100));
    const time = new Date(at).toLocaleString(undefined, { timeZoneName: 'short' });
    const number = (v) => new Intl.NumberFormat(undefined, { maximumFractionDigits: 6 }).format(v);
    return `${time} · ${sign}${hours}:${minutes}:${secs} ${sparkText.reset} · `
        + `${number(r.used)}% ${sparkText.used} · ${number(pace)}% ${sparkText.pace} · ${number(r.used)} ${sparkText.points}`;
}

function sparkNearest(readings, fraction, x) {
    const distance = (r) => Math.abs(x(Date.parse(r.at)) / 300 - fraction);
    return readings.reduce((best, r, i) => distance(r) < distance(readings[best]) ? i : best, 0);
}

function sparkKeyboard(e, index, count, show) {
    const steps = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1 };
    if (!(e.key in steps)) return;
    e.preventDefault();
    show(Math.max(0, Math.min(count - 1, index + steps[e.key])));
}

function interactiveSpark(g, a, x, y) {
    const readings = a.readings.filter((r) => Number.isFinite(Date.parse(r.at)) && Number.isFinite(r.used));
    const text = el('div', { class: 'spark-tooltip', 'aria-live': 'polite', 'aria-atomic': 'true' });
    const box = el('div', { class: 'spark-container', role: 'group', 'aria-label': `${a.name}. ${sparkText.help}` }, g, text);
    if (!readings.length) return box;
    box.tabIndex = 0;
    const cross = svg('g', { class: 'spark-crosshair', visibility: 'hidden', 'aria-hidden': 'true' });
    const vertical = svg('line', { y1: 0, y2: 72 });
    const horizontal = svg('line', { x1: 0, x2: 300 });
    const point = svg('circle', { class: 'spark-point', r: 3 });
    cross.append(vertical, horizontal, point);
    g.append(cross);
    let index = -1;
    const show = (next) => {
        index = next;
        const r = readings[index];
        const px = x(Date.parse(r.at));
        const py = y(r.used);
        vertical.setAttribute('x1', px);
        vertical.setAttribute('x2', px);
        horizontal.setAttribute('y1', py);
        horizontal.setAttribute('y2', py);
        point.setAttribute('cx', px);
        point.setAttribute('cy', py);
        cross.setAttribute('visibility', 'visible');
        text.textContent = sparkReadout(a, r);
    };
    const select = (e) => {
        const rect = g.getBoundingClientRect();
        show(sparkNearest(readings, (e.clientX - rect.left) / rect.width, x));
    };
    g.addEventListener('pointerdown', (e) => {
        g.setPointerCapture(e.pointerId);
        select(e);
    });
    g.addEventListener('pointermove', select);
    g.addEventListener('click', select);
    box.addEventListener('keydown', (e) => sparkKeyboard(e, index, readings.length, show));
    box.addEventListener('focus', () => show(Math.max(0, index)));
    return box;
}
