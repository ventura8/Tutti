"""A QR code of a short text, made here (no library, no network): byte mode, error correction level L, versions 1 to 6
(up to 134 bytes, enough for a wireless-debugging pairing code), one fixed mask (any mask is valid; the choice only tunes
readability). Follows the structure of Project Nayuki's QR Code generator (MIT). The matrix is drawn on the terminal with
half-block characters and on the dashboard as SVG (tutti/web/devices.js)."""

QR_DATA = (0, 19, 34, 55, 80, 108, 136)   # data codewords per version at level L
QR_EC = (0, 7, 10, 15, 20, 26, 18)        # error-correction codewords per block
QR_BLOCKS = (0, 1, 1, 1, 1, 1, 2)


def _mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = ((z << 1) ^ ((z >> 7) * 0x11d)) & 0x1ff
        z ^= ((y >> i) & 1) * x
    return z & 0xff


def _divisor(degree):
    out = [0] * degree
    out[-1] = 1
    root = 1
    for _ in range(degree):
        for j in range(degree):
            out[j] = _mul(out[j], root)
            if j + 1 < degree:
                out[j] ^= out[j + 1]
        root = _mul(root, 0x02)
    return out


def _remainder(data, divisor):
    out = [0] * len(divisor)
    for b in data:
        factor = b ^ out.pop(0)
        out.append(0)
        for i, coef in enumerate(divisor):
            out[i] ^= _mul(coef, factor)
    return out


def _bits(data, version):
    total = QR_DATA[version]
    bits = []

    def put(val, n):
        bits.extend((val >> i) & 1 for i in range(n - 1, -1, -1))
    put(4, 4)
    put(len(data), 8)
    for b in data:
        put(b, 8)
    put(0, min(4, total * 8 - len(bits)))
    bits.extend([0] * (-len(bits) % 8))
    pad = 0xec
    while len(bits) < total * 8:
        put(pad, 8)
        pad ^= 0xec ^ 0x11
    return [int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]


def codewords(data, version):
    """The data codewords split into blocks, each with its error-correction codewords, interleaved."""
    words = _bits(data, version)
    n = QR_BLOCKS[version]
    size = QR_DATA[version] // n
    blocks = [words[k * size:(k + 1) * size] for k in range(n)]
    div = _divisor(QR_EC[version])
    ecs = [_remainder(b, div) for b in blocks]
    return [b[i] for i in range(size) for b in blocks] + [e[i] for i in range(QR_EC[version]) for e in ecs]


class _Grid:
    def __init__(self, size):
        self.size = size
        self.m = [[False] * size for _ in range(size)]
        self.fn = [[False] * size for _ in range(size)]

    def set(self, x, y, dark):
        if 0 <= x < self.size and 0 <= y < self.size:
            self.m[y][x] = dark
            self.fn[y][x] = True

    def square(self, cx, cy, radius, dark):
        """A square pattern centred on (cx, cy): `dark(d)` says which rings (by Chebyshev distance) are dark."""
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                self.set(cx + dx, cy + dy, dark(max(abs(dx), abs(dy))))


def _patterns(g, version):
    for i in range(g.size):
        g.set(6, i, i % 2 == 0)
        g.set(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (g.size - 4, 3), (3, g.size - 4)):
        g.square(cx, cy, 4, lambda d: d not in (2, 4))
    if version > 1:
        g.square(g.size - 7, g.size - 7, 2, lambda d: d != 1)


def _format(g):
    """Format bits: level L (01), mask 0, with their BCH code, in both copies, and the dark module."""
    fmt = 1 << 3
    rem = fmt
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    f = ((fmt << 10) | rem) ^ 0x5412

    def bit(i):
        return (f >> i) & 1 == 1
    for i in range(6):
        g.set(8, i, bit(i))
    g.set(8, 7, bit(6))
    g.set(8, 8, bit(7))
    g.set(7, 8, bit(8))
    for i in range(9, 15):
        g.set(14 - i, 8, bit(i))
    for i in range(8):
        g.set(g.size - 1 - i, 8, bit(i))
    for i in range(8, 15):
        g.set(8, g.size - 15 + i, bit(i))
    g.set(8, g.size - 8, True)


def _order(g):
    """The free modules in the zigzag order of the data: column pairs right to left, skipping the timing column 6."""
    out = []
    for right in range(g.size - 1, 0, -2):
        right = right - 1 if right <= 6 else right
        upward = (right + 1) & 2 == 0
        for vert in range(g.size):
            y = g.size - 1 - vert if upward else vert
            out += [(x, y) for x in (right, right - 1) if not g.fn[y][x]]
    return out


def matrix(text):
    """The QR code of `text` as rows of booleans (True is dark), or None when it is longer than version 6 holds."""
    data = text.encode('utf8')
    version = next((v for v in range(1, len(QR_DATA)) if len(data) <= QR_DATA[v] - 2), 0)
    if not version:
        return None
    g = _Grid(version * 4 + 17)
    _patterns(g, version)
    _format(g)
    words = codewords(data, version)
    for i, (x, y) in enumerate(_order(g)):   # data bits in order (the remainder stays light), then mask 0: (x + y) even flips
        dark = i < len(words) * 8 and (words[i >> 3] >> (7 - (i & 7))) & 1 == 1
        g.m[y][x] = dark != ((x + y) % 2 == 0)
    return g.m


def terminal(text, quiet=2):
    """The QR code as text: two rows of modules per line with half blocks, dark modules drawn as spaces on a light ground,
    so it scans on a dark terminal. None when the text is too long."""
    m = matrix(text)
    if m is None:
        return None
    n = len(m) + 2 * quiet
    grid = [[False] * n for _ in range(quiet)] + [[False] * quiet + row + [False] * quiet for row in m]
    grid += [[False] * n for _ in range(quiet + 1)]
    chars = {(False, False): '█', (True, False): '▄', (False, True): '▀', (True, True): ' '}
    return '\n'.join(''.join(chars[(grid[y][x], grid[y + 1][x])] for x in range(n)) for y in range(0, n, 2))
