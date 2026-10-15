"""Condense a fice_decode.py op log into named operations (helps comparing captures).

    python re/fice_summarize.py ops.txt
"""
import re
import sys


def rd(bits):
    i = bits.index("1", 1)
    return sum(int(x) << k for k, x in enumerate(bits[i + 1:i + 9]))


def main():
    ops = []
    for ln in open(sys.argv[1]):
        p = ln.split()
        if p[1] == "IDLE":
            ops.append(("IDLE", float(p[2]) / 1000, None))
        elif p[1] == "SEL":
            ops.append(("SEL", int(p[2]), None))
        elif p[1] == "WR":
            ops.append(("WR", int(p[2], 16), int(p[3].split("=")[1])))
        else:
            ops.append(("RD", rd(p[2]) if "1" in p[2][1:] else None, None))
    s = " ".join(
        {"IDLE": lambda v: "IDLE:%.1fms" % v, "SEL": lambda v: "S%d" % v,
         "WR": lambda v: "W%02X" % v, "RD": lambda v: "R%s" % ("--" if v is None else "%02X" % v)}[k](v)
        for k, v, _ in ops)
    pats = [
        (r"S3 R(\w\w) W10 S5 W01 S2 W02 W01 R83", r"PRE(r3=\1)"),
        (r"S2 W11 S436 W(\w\w) W(\w\w) W01 S2 R9F S436 W(\w\w) S2 R83", r"XWR(\1\2=\3)"),
        (r"S2 W11 S436 W(\w\w) W(\w\w) W01 S2 R9F S436 R(\w\w) S2 R83", r"XRD(\1\2->\3)"),
        (r"S2 W22 S436 W(\w\w) W01 S2 R98 S436 W(\w\w) S2 R83", r"SFRWR(\1=\2)"),
        (r"S436 W02 R0D W(\w\w) W(\w\w) W(\w\w)((?: W\w\w){1,256}?)(?= S| IDLE|$)", r"PROG(\1\2 n=\3)"),
        (r"S436 W07 R0D WCB WB3 WC3 WFF", "ERASE_A(CB B3 C3 FF)"),
        (r"S436 W04 R0D WAE WB1 WC1 WFF", "ERASE_B(AE B1 C1 FF)"),
    ]
    for a, b in pats:
        s = re.sub(a, b, s)
    out, prev, n = [], None, 0
    for tok in s.split(" "):
        if tok.startswith("PROG(") and prev and prev.startswith("PROG("):
            n += 1
            continue
        if n:
            out.append("...+%d more PROG" % n)
            n = 0
        out.append(tok)
        prev = tok
    print("\n".join(out))


if __name__ == "__main__":
    main()
