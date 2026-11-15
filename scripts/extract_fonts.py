"""Extract u8g2 font byte arrays from the firmware's u8g2_fonts.c into fz/fonts/data/<name>.u8f.

usage: python scripts/extract_fonts.py third_party/u8g2_fonts.c font_name [font_name ...]
"""
import re, sys, pathlib

def c_string_bytes(lit: str) -> bytes:
    out = bytearray(); i = 0
    while i < len(lit):
        c = lit[i]
        if c != "\\":
            out.append(ord(c)); i += 1; continue
        n = lit[i + 1]
        if n in "01234567":
            j = i + 1; digits = ""
            while j < len(lit) and len(digits) < 3 and lit[j] in "01234567":
                digits += lit[j]; j += 1
            out.append(int(digits, 8)); i = j
        else:
            out.append({"n": 10, "t": 9, "r": 13, "\\": 92, '"': 34, "'": 39, "?": 63}[n]); i += 2
    return bytes(out)

def main():
    src = pathlib.Path(sys.argv[1]).read_text(encoding="latin-1")
    outdir = pathlib.Path(__file__).resolve().parent.parent / "fz" / "fonts" / "data"
    for name in sys.argv[2:]:
        m = re.search(r"const uint8_t u8g2_font_%s\[(\d+)\][^\n]*=\n" % re.escape(name), src)
        if not m:
            print("missing", name); continue
        size = int(m.group(1))
        body = src[m.end():]
        lits = re.match(r'(\s*"(?:[^"\\]|\\.)*")+', body).group(0)
        data = b"".join(c_string_bytes(s) for s in re.findall(r'"((?:[^"\\]|\\.)*)"', lits))
        assert len(data) + 1 == size, (name, len(data), size)  # array size includes trailing NUL
        (outdir / f"{name}.u8f").write_bytes(data)
        print(f"{name}: {len(data)} bytes")

main()
