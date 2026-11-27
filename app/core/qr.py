"""Minimal QR Code generator (byte mode, error correction L, versions 1-10 = up to 271 bytes) that returns an SVG.
Pure standard library, used for the 2FA set-up code, so no image or QR dependency is needed."""

# version -> (ec codewords per block, [(block count, data codewords per block), ...])
_BLOCKS = {1: (7, [(1, 19)]), 2: (10, [(1, 34)]), 3: (15, [(1, 55)]), 4: (20, [(1, 80)]), 5: (26, [(1, 108)]),
           6: (18, [(2, 68)]), 7: (20, [(2, 78)]), 8: (24, [(2, 97)]), 9: (30, [(2, 116)]), 10: (18, [(2, 68), (2, 69)])}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
          9: [6, 26, 46], 10: [6, 28, 50]}

_EXP, _LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _rs(data: list, n: int) -> list:
    gen = [1]
    for i in range(n):
        nxt = [0] * (len(gen) + 1)
        for j, c in enumerate(gen):
            nxt[j] ^= c
            if c:
                nxt[j + 1] ^= _EXP[_LOG[c] + i]
        gen = nxt
    rem = [0] * n
    for b in data:
        f = b ^ rem[0]
        rem = rem[1:] + [0]
        if f:
            for k in range(n):
                if gen[k + 1]:
                    rem[k] ^= _EXP[_LOG[gen[k + 1]] + _LOG[f]]
    return rem


def _capacity(v: int) -> int:
    return sum(c * d for c, d in _BLOCKS[v][1])


def _codewords(data: bytes, v: int) -> list:
    bits = "0100" + format(len(data), "08b" if v < 10 else "016b") + "".join(format(b, "08b") for b in data)
    cap = _capacity(v) * 8
    bits += "0" * min(4, cap - len(bits))
    bits += "0" * (-len(bits) % 8)
    words = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    while len(words) < _capacity(v):
        words.append(pad[(len(words) - len(bits) // 8) % 2])
    ec_n, groups = _BLOCKS[v]
    blocks, pos = [], 0
    for cnt, size in groups:
        for _ in range(cnt):
            blocks.append(words[pos:pos + size])
            pos += size
    ecs = [_rs(b, ec_n) for b in blocks]
    out = []
    for i in range(max(len(b) for b in blocks)):
        out += [b[i] for b in blocks if i < len(b)]
    for i in range(ec_n):
        out += [e[i] for e in ecs]
    return out


def _bch(value: int, poly: int, bits: int) -> int:
    r = value << bits
    top = poly.bit_length() - 1
    for i in range(r.bit_length() - 1, top - 1, -1):
        if r >> i & 1:
            r ^= poly << (i - top)
    return (value << bits) | r


_MASKS = [lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0, lambda x, y: (x + y) % 3 == 0,
          lambda x, y: (x // 3 + y // 2) % 2 == 0, lambda x, y: x * y % 2 + x * y % 3 == 0,
          lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0, lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0]


def _penalty(m: list) -> int:
    n, score = len(m), 0
    for grid in (m, [list(r) for r in zip(*m)]):
        for row in grid:
            run = 1
            for i in range(1, n):
                if row[i] == row[i - 1]:
                    run += 1
                else:
                    score += run - 2 if run >= 5 else 0
                    run = 1
            score += run - 2 if run >= 5 else 0
            s = "".join("1" if c else "0" for c in row)
            score += 40 * (s.count("10111010000") + s.count("00001011101"))
    for y in range(n - 1):
        for x in range(n - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(sum(r) for r in m) * 100 // (n * n)
    return score + 10 * (abs(dark - 50) // 5)


def matrix(text: str) -> list:
    data = text.encode("utf-8")
    v = next((v for v in range(1, 11) if 4 + (8 if v < 10 else 16) + 8 * len(data) <= _capacity(v) * 8), None)
    if v is None:
        raise ValueError("text too long for the built-in QR encoder")
    n = 17 + 4 * v
    mod = [[False] * n for _ in range(n)]
    fn = [[False] * n for _ in range(n)]

    def put(x, y, dark, func=True):
        mod[y][x] = bool(dark)
        if func:
            fn[y][x] = True

    for i in range(n):                                   # timing
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):      # finders + separators
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < n and 0 <= y < n:
                    d = max(abs(dx), abs(dy))
                    put(x, y, d not in (2, 4))
    pos = _ALIGN[v]
    for i, ay in enumerate(pos):                         # alignment
        for j, ax in enumerate(pos):
            if (i == 0 and j == 0) or (i == 0 and j == len(pos) - 1) or (i == len(pos) - 1 and j == 0):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
    for i in range(9):                                   # reserve format areas
        fn[8][i] = fn[i][8] = True
    for i in range(8):
        fn[8][n - 1 - i] = fn[n - 1 - i][8] = True
    if v >= 7:
        vb = _bch(v, 0x1F25, 12)
        for i in range(18):
            a, b = n - 11 + i % 3, i // 3
            put(a, b, vb >> i & 1)
            put(b, a, vb >> i & 1)
    # data, zigzag
    cw = _codewords(data, v)
    bits = [(w >> (7 - k)) & 1 for w in cw for k in range(8)]
    i, right = 0, n - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(n):
            for j in range(2):
                x = right - j
                y = n - 1 - vert if ((right + 1) & 2) == 0 else vert
                if not fn[y][x]:
                    mod[y][x] = bool(bits[i]) if i < len(bits) else False
                    i += 1
        right -= 2
    best = None
    for mi, fnc in enumerate(_MASKS):
        m = [row[:] for row in mod]
        for y in range(n):
            for x in range(n):
                if not fn[y][x] and fnc(x, y):
                    m[y][x] = not m[y][x]
        fmt = _bch((0b01 << 3) | mi, 0x537, 10) ^ 0x5412
        bit = [bool(fmt >> k & 1) for k in range(15)]
        for k in range(6):
            m[k][8] = bit[k]
        m[7][8], m[8][8], m[8][7] = bit[6], bit[7], bit[8]
        for k in range(9, 15):
            m[8][14 - k] = bit[k]
        for k in range(8):
            m[8][n - 1 - k] = bit[k]
        for k in range(8, 15):
            m[n - 15 + k][8] = bit[k]
        m[n - 8][8] = True
        sc = _penalty(m)
        if best is None or sc < best[0]:
            best = (sc, m)
    return best[1]


def svg(text: str, border: int = 4) -> str:
    m = matrix(text)
    n = len(m)
    d = "".join(f"M{x + border},{y + border}h1v1h-1z" for y in range(n) for x in range(n) if m[y][x])
    s = n + 2 * border
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {s} {s}" shape-rendering="crispEdges">'
            f'<rect width="{s}" height="{s}" fill="#fff"/><path d="{d}" fill="#000"/></svg>')
