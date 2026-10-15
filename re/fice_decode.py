"""Decode a Saleae Logic 2 CSV export of the Fortior FICE bus into an operation log.

    python re/fice_decode.py capture.csv[.xz|.gz] [--ck 1] [--dat 2] [--rate 24e6] [--hex fw.hex] [-o ops.txt]

--ck / --dat are CSV column numbers (column 0 is the time). The log has one line per frame:

    <time s>  SEL <reg>              register select   (S, 1, 1, 9-bit reg LSB first)
    <time s>  WR <byte> [ack=<n>]    write             (S, 1, 0,0,0, byte LSB first, then ack wait)
    <time s>  RD <bits>              read              (S, 0, target drives the remaining bits)
    <time s>  IDLE <us>              long gap (> 1 ms) between frames

With --hex, program bursts (SEL 436, WR 02, RD, WR addrH, WR addrL, WR count, data...) are
rebuilt and compared against the Intel HEX file.
"""
import argparse
import sys

import numpy as np

SYNC_LOW = 70     # FICEK low time (samples @24 MHz) that marks a frame start
FAST_LOW = 20     # low time at or below this is a "fast" cell


def load_csv(path, ck_col, dat_col, rate):
    a = np.loadtxt(path, delimiter=",", skiprows=1, usecols=(0, ck_col, dat_col))
    t = np.rint(a[:, 0] * rate).astype(np.int64)
    return t, a[:, 1].astype(np.int8), a[:, 2].astype(np.int8)


def cells(t, ck, d):
    """One cell per FICEK falling edge: (t_fall, low, bit_at_rise, end_time)."""
    fi = np.flatnonzero((ck[1:] == 0) & (ck[:-1] == 1)) + 1
    ri = np.flatnonzero((ck[1:] == 1) & (ck[:-1] == 0)) + 1
    j = np.searchsorted(ri, fi)
    out = []
    for k in range(len(fi) - 1):
        if j[k] >= len(ri) or ri[j[k]] >= fi[k + 1]:
            continue
        r = ri[j[k]]
        out.append((int(t[fi[k]]), int(t[r] - t[fi[k]]), int(d[r - 1]), int(t[fi[k + 1]])))
    return out


def lsb(bits):
    return sum(b << i for i, b in enumerate(bits))


def frames(cs):
    fr, cur = [], []
    for c in cs:
        if c[1] >= SYNC_LOW and cur:
            fr.append(cur)
            cur = []
        cur.append(c)
    if cur:
        fr.append(cur)
    return fr


def classify(f):
    rest = f[1:]
    if not rest:
        return ("IDLE", None, None)
    bits = [c[2] for c in rest]
    fast = [c[1] <= FAST_LOW for c in rest]
    if bits[0] == 1 and len(rest) > 1 and fast[1] and bits[1] == 1:
        w = [c[2] for c in rest[2:] if c[1] > FAST_LOW]
        return ("SEL", lsb(w), None)
    if bits[0] == 1:
        w = [c[2] for c in rest[1:] if c[1] > FAST_LOW]
        tail = [c[2] for c in rest[1:] if c[1] <= FAST_LOW]
        return ("WR", lsb(w[3:11]), len(tail))
    return ("RD", "".join(map(str, bits)), None)


def read_hex(path):
    mem, base = {}, 0
    for ln in open(path):
        ln = ln.strip()
        if not ln.startswith(":"):
            continue
        b = bytes.fromhex(ln[1:])
        n, a, typ = b[0], (b[1] << 8) | b[2], b[3]
        if typ == 0:
            for i, x in enumerate(b[4:4 + n]):
                mem[base + a + i] = x
        elif typ == 4:
            base = ((b[4] << 8) | b[5]) << 16
    return mem


def rebuild(ops):
    img, n, i = {}, 0, 0
    while i < len(ops) - 5:
        o = ops[i]
        if (o[1] == "SEL" and o[2] == 436 and ops[i + 1][1] == "WR" and ops[i + 1][2] == 2
                and ops[i + 2][1] == "RD"):
            h = [ops[i + 3 + k][2] for k in range(3)]
            addr, cnt, j = (h[0] << 8) | h[1], h[2] or 256, i + 6
            for k in range(cnt):
                img[addr + k] = ops[j][2]
                j += 1
            n += 1
            i = j
            continue
        i += 1
    return img, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--ck", type=int, default=1, help="CSV column of FICEK (default 1)")
    ap.add_argument("--dat", type=int, default=2, help="CSV column of FICED (default 2)")
    ap.add_argument("--rate", type=float, default=24e6)
    ap.add_argument("--hex", help="Intel HEX that was programmed, to verify against")
    ap.add_argument("-o", "--out", help="write the op log here (default: stdout)")
    a = ap.parse_args()

    t, ck, d = load_csv(a.csv, a.ck, a.dat, a.rate)
    fr = frames(cells(t, ck, d))
    ops = []
    for f in fr:
        kind, v, x = classify(f)
        ops.append((f[0][0] / a.rate, kind, v, x, f[-1][0] + f[-1][1]))  # end = last rising edge

    out = open(a.out, "w") if a.out else sys.stdout
    prev_end = None
    for tm, kind, v, x, end in ops:
        if prev_end is not None and (tm * a.rate - prev_end) > a.rate / 1000:
            out.write("%.6f  IDLE %.0f us\n" % (prev_end / a.rate, (tm * a.rate - prev_end) / a.rate * 1e6))
        if kind == "SEL":
            out.write("%.6f  SEL %d\n" % (tm, v))
        elif kind == "WR":
            out.write("%.6f  WR %02X ack=%d\n" % (tm, v, x))
        elif kind == "RD":
            out.write("%.6f  RD %s\n" % (tm, v))
        prev_end = end
    if a.out:
        out.close()

    from collections import Counter
    c = Counter(o[1] for o in ops)
    print("frames: %d  (SEL %d, WR %d, RD %d)" % (len(ops), c["SEL"], c["WR"], c["RD"]), file=sys.stderr)
    if a.hex:
        mem = read_hex(a.hex)
        img, n = rebuild(ops)
        bad = [x for x in mem if img.get(x) != mem[x]]
        extra = sorted(x for x in img if x not in mem)
        print("program bursts: %d, hex bytes: %d, rebuilt: %d, mismatched/missing: %d" %
              (n, len(mem), len(img), len(bad)), file=sys.stderr)
        if extra:
            print("bytes written that are not in the hex: " +
                  " ".join("%04X=%02X" % (x, img[x]) for x in extra), file=sys.stderr)


if __name__ == "__main__":
    main()
