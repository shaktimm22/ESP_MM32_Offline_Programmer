"""First-look analysis of a PulseView/sigrok .sr capture of the FICE bus.

    python re/sr_inspect.py capture.sr [--ck 0] [--dat 1] [--gap-us 20] [--bursts 40]

Prints the capture info, clock timing, which clock edge FICED changes on, and the bus
split into bursts (separated by --gap-us of clock silence). Each burst lists FICED sampled on
the rising and on the falling FICEK edge, as a bit string and as MSB-first / LSB-first hex.
"""
import argparse
import configparser
import re
import sys
import zipfile

import numpy as np


def load_sr(path):
    """Returns (samples uint8/uint16 array, samplerate Hz, {channel index: name})."""
    with zipfile.ZipFile(path) as z:
        meta = configparser.ConfigParser()
        meta.read_string(z.read("metadata").decode())
        dev = meta["device 1"]
        rate = parse_rate(dev["samplerate"])
        unit = int(dev.get("unitsize", "1"))
        prefix = dev["capturefile"]
        names = {int(k[5:]) - 1: v for k, v in dev.items() if re.fullmatch(r"probe\d+", k)}
        chunks = sorted((n for n in z.namelist() if n == prefix or n.startswith(prefix + "-")),
                        key=lambda n: int(n.rsplit("-", 1)[1]) if n != prefix else 0)
        raw = b"".join(z.read(n) for n in chunks)
    dtype = {1: np.uint8, 2: np.uint16, 4: np.uint32}[unit]
    return np.frombuffer(raw, dtype=dtype), rate, names


def parse_rate(s):
    m = re.fullmatch(r"\s*([\d.]+)\s*([kMG]?)Hz\s*", s)
    if not m:
        sys.exit(f"cannot parse samplerate '{s}'")
    return float(m.group(1)) * {"": 1, "k": 1e3, "M": 1e6, "G": 1e9}[m.group(2)]


def us(samples, rate):
    return samples * 1e6 / rate


def bits_to_hex(bits, msb_first):
    out = []
    for i in range(0, len(bits) - len(bits) % 8, 8):
        b = bits[i:i + 8] if msb_first else bits[i:i + 8][::-1]
        out.append("%02X" % int("".join(map(str, b)), 2))
    rest = len(bits) % 8
    return " ".join(out) + (f" +{rest}b" if rest else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--ck", type=int, default=0, help="FICEK channel index (default 0 = D0)")
    ap.add_argument("--dat", type=int, default=1, help="FICED channel index (default 1 = D1)")
    ap.add_argument("--gap-us", type=float, default=20.0, help="clock silence that splits bursts")
    ap.add_argument("--bursts", type=int, default=40, help="how many bursts to print (0 = all)")
    a = ap.parse_args()

    data, rate, names = load_sr(a.file)
    n = len(data)
    print(f"{a.file}: {n} samples @ {rate / 1e6:g} MHz = {n / rate:.3f} s")
    print("channels:", ", ".join(f"D{i}={v}" for i, v in sorted(names.items())))

    for ch, nm in sorted(names.items()):
        s = (data >> ch) & 1
        e = np.count_nonzero(np.diff(s))
        print(f"  D{ch} {nm:<10} start={s[0]} end={s[-1]} edges={e}")

    ck = ((data >> a.ck) & 1).astype(np.int8)
    dat = ((data >> a.dat) & 1).astype(np.int8)
    d = np.diff(ck)
    rise = np.flatnonzero(d == 1) + 1
    fall = np.flatnonzero(d == -1) + 1
    if len(rise) < 2:
        print("FICEK has (almost) no edges - wrong channel? try --ck")
        return

    # Clock timing (pulse widths only within bursts).
    edges = np.sort(np.concatenate([rise, fall]))
    widths = np.diff(edges)
    gap = a.gap_us * rate / 1e6
    inb = widths[widths < gap]
    if len(inb):
        print(f"\nFICEK half-period inside bursts: min {us(inb.min(), rate):.3f} us, "
              f"median {us(np.median(inb), rate):.3f} us, max {us(inb.max(), rate):.3f} us "
              f"(min {inb.min()} samples)")
        if inb.min() < 4:
            print("  WARNING: under 4 samples per half-period - capture again at a higher sample rate")

    # Which FICEK phase does FICED change in?
    dd = np.flatnonzero(np.diff(dat)) + 1
    ck_at = ck[dd]
    print(f"FICED edges: {len(dd)} total, {np.count_nonzero(ck_at == 0)} while FICEK low, "
          f"{np.count_nonzero(ck_at == 1)} while FICEK high")
    print("  (changes while low -> sampled on rising edge; while high -> sampled on falling edge)")

    # Bursts.
    breaks = np.flatnonzero(widths >= gap)
    starts = np.concatenate([[0], breaks + 1])
    ends = np.concatenate([breaks, [len(edges) - 1]])
    print(f"\n{len(starts)} bursts (gap >= {a.gap_us:g} us)")
    limit = len(starts) if a.bursts == 0 else min(a.bursts, len(starts))
    for k in range(limit):
        e0, e1 = edges[starts[k]], edges[ends[k]]
        r = rise[(rise >= e0) & (rise <= e1)]
        f = fall[(fall >= e0) & (fall <= e1)]
        br, bf = dat[r].tolist(), dat[np.minimum(f, n - 1)].tolist()
        print(f"\n#{k} t={us(e0, rate) / 1000:.3f} ms len={us(e1 - e0, rate):.1f} us "
              f"rise={len(r)} fall={len(f)}")
        for tag, bits in (("rise", br), ("fall", bf)):
            s = "".join(map(str, bits))
            print(f"  {tag} bits: {s[:160]}{'...' if len(s) > 160 else ''}")
            print(f"  {tag} msb : {bits_to_hex(bits, True)[:120]}")
            print(f"  {tag} lsb : {bits_to_hex(bits, False)[:120]}")
    if limit < len(starts):
        print(f"\n... {len(starts) - limit} more bursts (use --bursts 0)")


if __name__ == "__main__":
    main()
