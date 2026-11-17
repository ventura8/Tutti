// The browser tests' layout and accessibility checkers (tests/e2e/test_ui.py), run inside the page. Each returns a list of
// problems as plain strings ("<what> <where>: <numbers>"); an empty list passes. Nothing here changes the page.
(() => {
    const visible = (el) => {
        if (!(el instanceof Element)) return false;
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) return false;   // sr-only text (1 px) and collapsed boxes are not drawn
        // a closed <details>' content keeps a box but is not drawn
        if (!el.checkVisibility({ contentVisibilityAuto: true, opacityProperty: true, visibilityProperty: true })) return false;
        for (let n = el; n; n = n.parentElement) {
            const s = getComputedStyle(n);
            if (s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) === 0) return false;
            if (s.clip === 'rect(0px, 0px, 0px, 0px)' || s.clipPath === 'inset(50%)') return false;
        }
        return true;
    };
    const where = (el) => {
        const id = el.id ? `#${el.id}` : '';
        const cls = typeof el.className === 'string' && el.className ? `.${el.className.trim().split(/\s+/).join('.')}` : '';
        const text = (el.innerText || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 40);
        let card = el.closest('section[aria-labelledby], .card, header, nav, footer, form');
        const head = card ? (card.querySelector('h2, h3, legend')?.textContent || card.id || card.tagName).trim().slice(0, 30) : '';
        return `${el.tagName.toLowerCase()}${id}${cls}${text ? ` "${text}"` : ''}${head ? ` in [${head}]` : ''}`;
    };
    // a box inside a sideways scroller (the accounts row) is meant to run past the viewport: the scroller itself is checked
    const scroller = (el) => {
        for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
            const s = getComputedStyle(n);
            if (/(auto|scroll)/.test(s.overflowX) && n.scrollWidth > n.clientWidth + 1) return n;
        }
        return null;
    };
    // the part of a box a scroll container lets show: content scrolled out of a box that clips is not drawn there
    const clipped = (el, r) => {
        let { left, top, right, bottom } = r;
        for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
            const s = getComputedStyle(n);
            if (s.overflowX === 'visible' && s.overflowY === 'visible') continue;
            const c = n.getBoundingClientRect();
            if (s.overflowX !== 'visible') { left = Math.max(left, c.left); right = Math.min(right, c.right); }
            if (s.overflowY !== 'visible') { top = Math.max(top, c.top); bottom = Math.min(bottom, c.bottom); }
        }
        return right - left > 1 && bottom - top > 1 ? { left, top, right, bottom } : null;
    };
    const ownText = (el) => [...el.childNodes].some((n) => n.nodeType === Node.TEXT_NODE && n.textContent.trim());
    const CONTROLS = 'a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=link], [role=tab], [role=checkbox], [role=switch], [tabindex]:not([tabindex="-1"])';

    // text boxes: elements with their own text, measured by their text's line boxes (a Range), not the whole block
    const textBoxes = () => {
        const out = [];
        for (const el of document.body.querySelectorAll('*')) {
            if (!ownText(el) || !visible(el) || ['SCRIPT', 'STYLE', 'OPTION'].includes(el.tagName)) continue;
            for (const n of el.childNodes) {
                if (n.nodeType !== Node.TEXT_NODE || !n.textContent.trim()) continue;
                const range = document.createRange();
                range.selectNodeContents(n);
                for (const raw of range.getClientRects()) {
                    const r = raw.width > 1 && raw.height > 1 ? clipped(el, raw) : null;
                    if (r) out.push({ el, r });
                }
            }
        }
        return out;
    };

    function noHorizontalScroll() {
        const d = document.documentElement;
        return d.scrollWidth > d.clientWidth + 1 ? [`the page scrolls sideways: ${d.scrollWidth} px of content in ${d.clientWidth} px`] : [];
    }

    // a bar fixed to the bottom must not cover the page's last content when scrolled to the end
    function floatingClearance() {
        const out = [];
        const fixed = [...document.body.querySelectorAll('*')].filter((el) => visible(el) && getComputedStyle(el).position === 'fixed');
        if (!fixed.length) return out;
        window.scrollTo(0, document.documentElement.scrollHeight);
        const last = [...document.body.querySelectorAll('*')].filter((el) => visible(el) && !floating(el) && (ownText(el) || el.matches(CONTROLS)));
        for (const bar of fixed) {
            const b = bar.getBoundingClientRect();
            for (const el of last) {
                const r = el.getBoundingClientRect();
                if (r.bottom > b.top + 2 && r.top < b.bottom && r.right > b.left && r.left < b.right) {
                    out.push(`hidden under the fixed bar at the end of the page ${where(el)}`);
                }
            }
        }
        window.scrollTo(0, 0);
        return out;
    }

    function insideViewport() {
        const out = [];
        const w = document.documentElement.clientWidth;
        for (const el of document.body.querySelectorAll('*')) {
            if (!visible(el) || scroller(el)) continue;
            const r = el.getBoundingClientRect();
            if (r.left < -1 || r.right > w + 1) out.push(`past the viewport's edge ${where(el)}: ${Math.round(r.left)}–${Math.round(r.right)} of ${w} px`);
        }
        return out;
    }

    // clipped or cut-off text: a box that hides overflow while its content is larger, an ellipsis, a line clamp
    function noClippedText() {
        const out = [];
        for (const el of document.body.querySelectorAll('*')) {
            if (!visible(el) || !(el.innerText || '').trim()) continue;
            const s = getComputedStyle(el);
            const hidesX = /(hidden|clip)/.test(s.overflowX), hidesY = /(hidden|clip)/.test(s.overflowY);
            if (s.textOverflow === 'ellipsis' && el.scrollWidth > el.clientWidth + 1) out.push(`text truncated with an ellipsis ${where(el)}`);
            else if (hidesX && el.scrollWidth > el.clientWidth + 1) out.push(`text cut off sideways ${where(el)}: ${el.scrollWidth} > ${el.clientWidth} px`);
            else if (hidesY && el.scrollHeight > el.clientHeight + 1) out.push(`text cut off at the bottom ${where(el)}: ${el.scrollHeight} > ${el.clientHeight} px`);
            if (s.webkitLineClamp && s.webkitLineClamp !== 'none' && el.scrollHeight > el.clientHeight + 1) out.push(`text line-clamped ${where(el)}`);
        }
        return out;
    }

    // a fixed or sticky bar (the phone's bottom tab bar, the app bar) floats over content by design while the page scrolls
    const floating = (el) => {
        for (let n = el; n && n !== document.body; n = n.parentElement) {
            if (/(fixed|sticky)/.test(getComputedStyle(n).position)) return n;
        }
        return null;
    };

    // overlap: two pieces of text, two controls, or a control and text, drawn over each other (not one inside the other).
    // Measured at the top of the page; content under a floating bar is checked by floatingClearance.
    function noOverlap() {
        window.scrollTo(0, 0);
        const boxes = textBoxes().filter((b) => !floating(b.el)).map((b) => ({ ...b, fixed: false }));
        for (const el of document.body.querySelectorAll(CONTROLS)) {
            const r = visible(el) && !floating(el) ? clipped(el, el.getBoundingClientRect()) : null;
            if (r) boxes.push({ el, r, control: true, fixed: false });
        }
        const bars = [...document.body.querySelectorAll('*')].filter((el) => visible(el) && floating(el) === el);
        for (const bar of bars) {   // a bar's own text and controls against the page's top, where nothing has scrolled under it
            for (const el of bar.querySelectorAll('*')) {
                if (!visible(el)) continue;
                const fixed = getComputedStyle(floating(el)).position === 'fixed';   // its nearest bar: the nav inside the app bar
                if (el.matches(CONTROLS)) boxes.push({ el, r: el.getBoundingClientRect(), control: true, fixed });
                else if (ownText(el)) boxes.push({ el, r: el.getBoundingClientRect(), fixed });
            }
        }
        const out = [];
        const seen = new Set();
        for (let i = 0; i < boxes.length; i++) {
            for (let j = i + 1; j < boxes.length; j++) {
                const a = boxes[i], b = boxes[j];
                if (a.el === b.el || a.el.contains(b.el) || b.el.contains(a.el)) continue;
                if (a.fixed !== b.fixed) continue;   // a bar fixed to the viewport floats over the page: floatingClearance checks it
                const x = Math.min(a.r.right, b.r.right) - Math.max(a.r.left, b.r.left);
                const y = Math.min(a.r.bottom, b.r.bottom) - Math.max(a.r.top, b.r.top);
                if (x <= 2 || y <= 2) continue;
                const key = `${where(a.el)}|${where(b.el)}`;
                if (seen.has(key)) continue;
                seen.add(key);
                out.push(`overlap ${where(a.el)} and ${where(b.el)}: ${Math.round(x)}×${Math.round(y)} px`);
            }
        }
        return out;
    }

    // WCAG 2.5.8: every target at least 24×24 CSS px, except a link inside a sentence
    function targetSize() {
        const out = [];
        for (const el of document.body.querySelectorAll(CONTROLS)) {
            if (!visible(el) || scroller(el) === el) continue;
            const r = el.getBoundingClientRect();
            const inline = el.tagName === 'A' && getComputedStyle(el).display === 'inline' && el.parentElement &&
                [...el.parentElement.childNodes].some((n) => n !== el && n.textContent.trim());
            if (inline) continue;
            if (r.width < 24 || r.height < 24) out.push(`target smaller than 24 px ${where(el)}: ${Math.round(r.width)}×${Math.round(r.height)}`);
        }
        return out;
    }

    // an icon button (no visible words) needs a name a screen reader can say
    function iconNames() {
        const out = [];
        for (const el of document.body.querySelectorAll('button, a[href], [role=button]')) {
            if (!visible(el)) continue;
            const words = (el.innerText || '').replace(/[^\p{L}\p{N}]/gu, '');
            if (words.length >= 2) continue;
            const named = el.getAttribute('aria-label') || el.getAttribute('title') ||
                (el.getAttribute('aria-labelledby') || '').split(' ').map((id) => document.getElementById(id)?.textContent || '').join('').trim();
            if (!named || !named.trim()) out.push(`icon control without a spoken name ${where(el)}`);
        }
        return out;
    }

    function landmarks() {
        const out = [];
        const count = (sel) => [...document.querySelectorAll(sel)].filter((e) => !e.closest('[hidden]')).length;
        if (count('main, [role=main]') !== 1) out.push(`expected one main landmark, found ${count('main, [role=main]')}`);
        if (count('h1') !== 1) out.push(`expected one h1, found ${count('h1')}`);
        if (document.querySelector('header.appbar') && count('nav[aria-label]') < 1) out.push('the views navigation has no label');
        let last = 1;
        for (const h of document.querySelectorAll('h1, h2, h3, h4, h5, h6')) {
            if (!visible(h)) continue;
            const level = Number(h.tagName[1]);
            if (level > last + 1) out.push(`heading level skips from h${last} to h${level} ${where(h)}`);
            last = level;
        }
        return out;
    }

    function liveRegions() {
        const live = [...document.querySelectorAll('[aria-live], [role=status], [role=alert], output')];
        return live.length ? [] : ['no live region announces updates'];
    }

    // reduced motion: nothing animates or transitions for longer than a blink
    function reducedMotion() {
        const out = [];
        const secs = (v) => Math.max(...v.split(',').map((x) => (x.trim().endsWith('ms') ? parseFloat(x) / 1000 : parseFloat(x)) || 0));
        for (const el of document.querySelectorAll('*')) {
            const s = getComputedStyle(el);
            const anim = s.animationName !== 'none' ? secs(s.animationDuration) * (s.animationIterationCount === 'infinite' ? 100 : 1) : 0;
            const tr = secs(s.transitionDuration);
            if (anim > 0.01 || tr > 0.01) out.push(`moves under reduced motion ${where(el)}: animation ${anim}s, transition ${tr}s`);
        }
        return out;
    }

    // the controls a keyboard should reach, in the page's order
    function tabbables() {
        return [...document.querySelectorAll(CONTROLS)].filter((el) => visible(el) && !el.disabled && el.tabIndex >= 0 && !el.closest('[inert]'));
    }

    function focusShown(el) {
        const s = getComputedStyle(el);
        const outline = s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) >= 1;
        const ring = s.boxShadow && s.boxShadow !== 'none';
        return outline || ring;
    }

    // WCAG 1.4.12: the text spacing a user's stylesheet may set
    function textSpacing() {
        const st = document.createElement('style');
        st.id = 'wcag-text-spacing';
        st.textContent = '* { line-height: 1.5 !important; letter-spacing: 0.12em !important; word-spacing: 0.16em !important; } p { margin-bottom: 2em !important; }';
        document.head.append(st);
        return [];
    }

    // the sequential focus starting point at the very top of the page, as on a fresh load
    function startAtTop() {
        document.activeElement?.blur();
        window.scrollTo(0, 0);
        const s = document.createElement('span');
        s.tabIndex = -1;
        document.body.prepend(s);
        s.focus();
        s.addEventListener('blur', () => s.remove(), { once: true });
        return [];
    }

    // the words on screen: no leaked null/undefined/NaN, no empty list before a full stop, no "1 accounts" or "1 account need"
    // (the dashboard audit, 2026-11-16: "nullnullnull" under What did I miss?, "Since <time>: .", "1 account need a fresh reading")
    const COPY = [
        [/\b(null|undefined|NaN|\[object Object\])\b/, 'a leaked script value'],
        [/:\s*\.(\s|$)/, 'an empty list before a full stop'],
        [/(^|[^\d.,])1 (account|job|machine|worker|task|device|user|person|agent)s\b/, 'a plural after 1'],
        [/(^|[^\d.,])1 (account|job|machine|worker|task|device|user|person|agent) (need|have|are|were)\b/, 'a plural verb after 1'],
        [/\b([2-9]|\d\d+) (account|job|machine|worker|task|device|user|agent) (needs|has|is|was)\b/, 'a singular verb after a count'],
        [/\b([2-9]|\d\d+) stalled: open its\b/, '"its" for several'],
        [/\w\(s\)/, 'a "(s)" plural: say the count in words'],
    ];
    function copyLint() {
        const out = [];
        for (const { el } of textBoxes()) {
            const text = [...el.childNodes].filter((n) => n.nodeType === Node.TEXT_NODE).map((n) => n.textContent).join('').replace(/\s+/g, ' ');
            for (const [re, what] of COPY) if (re.test(text)) out.push(`${what}: "${text.trim().slice(0, 80)}" at ${where(el)}`);
        }
        return [...new Set(out)];
    }

    // a headline (a tile's value, a card's title) that wraps past two lines is a list crammed into a headline
    function headlinesFit() {
        const out = [];
        for (const el of document.querySelectorAll('.tile__value, .card__head h2')) {
            if (!visible(el)) continue;
            const lh = parseFloat(getComputedStyle(el).lineHeight) || 1.25 * parseFloat(getComputedStyle(el).fontSize);
            const lines = Math.round(el.getBoundingClientRect().height / lh);
            if (lines > 2) out.push(`a headline over ${lines} lines: ${where(el)}`);
        }
        // a tile's note is one or two short sentences: the long explanation belongs in its Details
        for (const el of document.querySelectorAll('.tile > .tile__note')) {
            if (visible(el) && el.textContent.length > 140) out.push(`a tile note of ${el.textContent.length} characters: ${where(el)}`);
        }
        return out;
    }

    window.tuttiChecks = { copyLint, headlinesFit, startAtTop, noHorizontalScroll, insideViewport, noClippedText, noOverlap, targetSize, iconNames, landmarks, liveRegions,
        reducedMotion, tabbables: () => tabbables().map(where), focusShown: () => (document.activeElement ? focusShown(document.activeElement) : false),
        active: () => (document.activeElement && document.activeElement !== document.body ? where(document.activeElement) : 'body'), textSpacing,
        layout: () => [...noHorizontalScroll(), ...insideViewport(), ...noClippedText(), ...noOverlap(), ...floatingClearance()] };
})();
