#!/usr/bin/env python3
"""Check portfolio tile SVGs against the site's hard rules, then render PNGs.

Usage: python3 check_tile_svgs.py <slug> <file.svg> [<file.svg> ...]
Renders with rsvg-convert (Homebrew librsvg). Exit code 1 if any check fails.
"""
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

XMLNS = 'xmlns="http://www.w3.org/2000/svg"'
NS = "{http://www.w3.org/2000/svg}"
BANNED = [r"<script", r"<text", r"<image", r"<foreignObject", r"<style", r"@import",
          r"\son[a-z]+=", r"http"]
MAX_BYTES = 60 * 1024
DECIMALS = re.compile(r"\d\.\d{3,}")


def check(slug, path, seen_ids):
    errs = []
    raw = open(path, encoding="utf-8").read()
    size = len(raw.encode())
    print(f"\n{path}: {size} bytes ({size / 1024:.1f} KB)")
    # a) XML + viewBox
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        return [f"not valid XML: {e}"]
    if root.tag != NS + "svg":
        errs.append(f"root is {root.tag}, not namespaced <svg>")
    if root.get("viewBox") != "0 0 800 600":
        errs.append(f"viewBox is {root.get('viewBox')!r}, not '0 0 800 600'")
    if root.get("width") or root.get("height"):
        errs.append("root has width/height; only viewBox is allowed")
    first = list(root)[0] if len(root) else None
    if first is None or first.tag != NS + "rect" or first.get("width") != "800" \
            or first.get("height") != "600" or first.get("x", "0") != "0" or first.get("y", "0") != "0" \
            or first.get("fill") in (None, "none", "transparent"):
        errs.append("first element is not a filled full-bleed 800x600 <rect>")
    # b) banned content (xmlns URL removed first)
    body = raw.replace(XMLNS, "", 1)
    for pat in BANNED:
        m = re.search(pat, body, re.I if pat != r"\son[a-z]+=" else 0)
        if m:
            errs.append(f"banned content {pat!r} found: {body[max(0, m.start()-20):m.end()+20]!r}")
    # c) ids
    ids = [el.get("id") for el in root.iter() if el.get("id")]
    for i in ids:
        if not i.startswith(slug + "-"):
            errs.append(f"id {i!r} lacks prefix '{slug}-'")
        if i in seen_ids:
            errs.append(f"id {i!r} also used in {seen_ids[i]}")
        seen_ids.setdefault(i, path)
    # d) size
    if size > MAX_BYTES:
        errs.append(f"{size} bytes is over 60 KB")
    # rounding (stated rule: at most 2 decimals)
    m = DECIMALS.search(raw)
    if m:
        errs.append(f"coordinate with >2 decimals: {m.group()}")
    print(f"  ids: {ids}")
    return errs


def render(path, outdir):
    exe = shutil.which("rsvg-convert")
    if not exe:
        return ["rsvg-convert not installed; cannot render"]
    errs = []
    base = os.path.splitext(os.path.basename(path))[0]
    for w, h in ((146, 110), (800, 600)):
        out = os.path.join(outdir, f"{base}-{w}x{h}.png")
        p = subprocess.run([exe, "-w", str(w), "-h", str(h), "-o", out, path],
                           capture_output=True, text=True)
        if p.returncode != 0 or not os.path.exists(out):
            errs.append(f"render {w}x{h} failed: {p.stderr.strip()}")
        else:
            print(f"  rendered {out}")
    return errs


def main():
    slug, files = sys.argv[1], sys.argv[2:]
    seen, failed = {}, False
    for f in files:
        errs = check(slug, f, seen)
        if not errs:
            errs += render(f, os.path.dirname(os.path.abspath(f)))
        for e in errs:
            print(f"  FAIL: {e}")
        print("  PASS" if not errs else "  => FAILED")
        failed |= bool(errs)
    print("\nRESULT:", "FAIL" if failed else "PASS")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
