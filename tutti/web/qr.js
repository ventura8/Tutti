// Tutti dashboard: a QR code of an invite link, drawn as SVG on this page (no library, no network). Byte mode, error
// correction level L, versions 1 to 6 (up to 134 bytes, enough for an invite link), one fixed mask (any mask is valid;
// the choice only tunes readability). Follows the structure of Project Nayuki's QR Code generator (MIT).
'use strict';

const QR_DATA = [0, 19, 34, 55, 80, 108, 136];   // data codewords per version at level L
const QR_EC = [0, 7, 10, 15, 20, 26, 18];        // error-correction codewords per block
const QR_BLOCKS = [0, 1, 1, 1, 1, 1, 2];

function qrMul(x, y) {
    let z = 0;
    for (let i = 7; i >= 0; i--) {
        z = (z << 1) ^ ((z >>> 7) * 0x11d);
        z ^= ((y >>> i) & 1) * x;
    }
    return z;
}

function qrDivisor(degree) {
    const out = new Array(degree).fill(0);
    out[degree - 1] = 1;
    let root = 1;
    for (let i = 0; i < degree; i++) {
        for (let j = 0; j < out.length; j++) {
            out[j] = qrMul(out[j], root);
            if (j + 1 < out.length) out[j] ^= out[j + 1];
        }
        root = qrMul(root, 0x02);
    }
    return out;
}

function qrRemainder(data, divisor) {
    const out = divisor.map(() => 0);
    for (const b of data) {
        const factor = b ^ out.shift();
        out.push(0);
        divisor.forEach((coef, i) => { out[i] ^= qrMul(coef, factor); });
    }
    return out;
}

function qrCodewords(bytes, version) {
    const total = QR_DATA[version];
    const bits = [];
    const put = (val, len) => { for (let i = len - 1; i >= 0; i--) bits.push((val >>> i) & 1); };
    put(4, 4);
    put(bytes.length, 8);
    bytes.forEach((b) => put(b, 8));
    put(0, Math.min(4, total * 8 - bits.length));
    while (bits.length % 8) bits.push(0);
    let pad = 0xec;
    while (bits.length < total * 8) {
        put(pad, 8);
        pad ^= 0xec ^ 0x11;
    }
    const data = [];
    for (let i = 0; i < bits.length; i += 8) data.push(bits.slice(i, i + 8).reduce((a, b) => (a << 1) | b, 0));
    const n = QR_BLOCKS[version];
    const size = total / n;
    const blocks = [];
    for (let k = 0; k < n; k++) blocks.push(data.slice(k * size, (k + 1) * size));
    const div = qrDivisor(QR_EC[version]);
    const ecs = blocks.map((b) => qrRemainder(b, div));
    const out = [];
    for (let i = 0; i < size; i++) blocks.forEach((b) => out.push(b[i]));
    for (let i = 0; i < QR_EC[version]; i++) ecs.forEach((e) => out.push(e[i]));
    return out;
}

function qrGrid(size) {
    const m = Array.from({ length: size }, () => new Array(size).fill(false));
    const fn = Array.from({ length: size }, () => new Array(size).fill(false));
    const set = (x, y, dark) => {
        if (x < 0 || x >= size || y < 0 || y >= size) return;
        m[y][x] = dark;
        fn[y][x] = true;
    };
    return { m, fn, set, size };
}

// a square pattern centred on (cx, cy): `dark(d)` says which rings (by Chebyshev distance) are dark
function qrSquare(g, cx, cy, radius, dark) {
    for (let dy = -radius; dy <= radius; dy++) {
        for (let dx = -radius; dx <= radius; dx++) g.set(cx + dx, cy + dy, dark(Math.max(Math.abs(dx), Math.abs(dy))));
    }
}

function qrPatterns(g, version) {
    const size = g.size;
    for (let i = 0; i < size; i++) {
        g.set(6, i, i % 2 === 0);
        g.set(i, 6, i % 2 === 0);
    }
    for (const [cx, cy] of [[3, 3], [size - 4, 3], [3, size - 4]]) qrSquare(g, cx, cy, 4, (d) => d !== 2 && d !== 4);
    if (version > 1) qrSquare(g, size - 7, size - 7, 2, (d) => d !== 1);
}

// format bits: level L (01), mask 0, with their BCH code, in both copies, and the dark module
function qrFormat(g) {
    const size = g.size;
    const fmt = 1 << 3;
    let rem = fmt;
    for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
    const f = ((fmt << 10) | rem) ^ 0x5412;
    const bit = (i) => ((f >>> i) & 1) === 1;
    for (let i = 0; i <= 5; i++) g.set(8, i, bit(i));
    g.set(8, 7, bit(6));
    g.set(8, 8, bit(7));
    g.set(7, 8, bit(8));
    for (let i = 9; i < 15; i++) g.set(14 - i, 8, bit(i));
    for (let i = 0; i < 8; i++) g.set(size - 1 - i, 8, bit(i));
    for (let i = 8; i < 15; i++) g.set(8, size - 15 + i, bit(i));
    g.set(8, size - 8, true);
}

// the column pairs right to left, skipping the vertical timing column 6
function qrColumns(size) {
    const out = [];
    for (let right = size - 1; right >= 1; right -= 2) out.push(right <= 6 ? right - 1 : right);
    return out;
}

// the free modules in the zigzag order of the data
function qrOrder(g) {
    const order = [];
    for (const right of qrColumns(g.size)) {
        const upward = ((right + 1) & 2) === 0;
        for (let vert = 0; vert < g.size; vert++) {
            const y = upward ? g.size - 1 - vert : vert;
            for (const x of [right, right - 1]) if (!g.fn[y][x]) order.push([x, y]);
        }
    }
    return order;
}

// The matrix (rows of booleans, true is dark) of `text`, or null when it is too long for version 6.
function qrMatrix(text) {
    const bytes = Array.from(new TextEncoder().encode(text));
    const version = QR_DATA.findIndex((d, v) => v > 0 && bytes.length <= d - 2);
    if (version < 1) return null;
    const g = qrGrid(version * 4 + 17);
    qrPatterns(g, version);
    qrFormat(g);
    const words = qrCodewords(bytes, version);
    // data bits in order (the remainder bits stay light), then mask 0: (x + y) even flips the module
    qrOrder(g).forEach(([x, y], i) => {
        const dark = i < words.length * 8 && ((words[i >>> 3] >>> (7 - (i & 7))) & 1) === 1;
        g.m[y][x] = dark !== ((x + y) % 2 === 0);
    });
    return g.m;
}

// An <svg> of the QR code with a quiet zone, or null when the text is too long.
function qrSvg(text, label) {
    const m = qrMatrix(text);
    if (!m) return null;
    const ns = 'http://www.w3.org/2000/svg';
    const n = m.length + 8;
    const node = document.createElementNS(ns, 'svg');
    node.setAttribute('viewBox', `0 0 ${n} ${n}`);
    node.setAttribute('class', 'qr');
    node.setAttribute('role', 'img');
    node.setAttribute('aria-label', label);
    const bg = document.createElementNS(ns, 'rect');
    bg.setAttribute('width', n);
    bg.setAttribute('height', n);
    bg.setAttribute('fill', '#fff');
    node.append(bg);
    let d = '';
    m.forEach((row, y) => row.forEach((dark, x) => { if (dark) d += `M${x + 4} ${y + 4}h1v1h-1z`; }));
    const path = document.createElementNS(ns, 'path');
    path.setAttribute('d', d);
    path.setAttribute('fill', '#000');
    node.append(path);
    return node;
}

if (typeof module !== 'undefined') module.exports = { qrMatrix };
