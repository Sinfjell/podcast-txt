#!/usr/bin/env python3
"""Regenerate the raster brand assets from the mark in static/favicon.svg.

Dev-only; not part of the app and not in requirements.txt. Needs:
  - headless Chromium (Playwright's build is found automatically; override with
    the CHROMIUM env var), which renders the SVG and the Schibsted Grotesk text
    from static/fonts/ exactly as the site does;
  - nothing else: the PNGs are written by Chromium and the .ico is assembled
    here from PNG frames.

Writes into static/: icon-192.png, icon-512.png, icon-maskable-512.png,
apple-touch-icon.png (180, opaque), favicon.ico (16/32/48) and og-image.png
(1200x630). Run from anywhere:  python3 ops/render-brand-assets.py
"""
import glob
import os
import re
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")
FONT = os.path.join(STATIC, "fonts", "schibsted-grotesk-latin.woff2")
SVG_NS = "{http://www.w3.org/2000/svg}"

# Mark bounding box inside the 32-unit favicon viewBox (group translate 1,2).
MARK_W, MARK_H, MARK_CX, MARK_CY = 26.0, 20.0, 16.0, 16.0


def read_mark():
    """Return (light colours by class, list of (class, rect attrs)) from favicon.svg."""
    path = os.path.join(STATIC, "favicon.svg")
    text = open(path, encoding="utf-8").read()
    # The first fill per class in <style> is the light-theme value.
    colours = {}
    for cls, fill in re.findall(r"\.(\w+)\s*\{\s*fill:\s*(#[0-9A-Fa-f]{6})", text):
        colours.setdefault(cls, fill)
    root = ET.fromstring(text)
    group = root.find(SVG_NS + "g")
    dx, dy = map(float, re.findall(r"-?[\d.]+", group.get("transform")))
    bars = []
    for r in group.findall(SVG_NS + "rect"):
        a = {k: float(r.get(k)) for k in ("x", "y", "width", "height", "rx")}
        a["x"] += dx
        a["y"] += dy
        bars.append((r.get("class"), a))
    return colours, bars


def mark_group(colours, bars, scale, cx, cy):
    """SVG <g> drawing the mark scaled by `scale` and centred on (cx, cy)."""
    tx, ty = cx - MARK_CX * scale, cy - MARK_CY * scale
    rects = "".join(
        '<rect fill="{c}" x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}"/>'.format(
            c=colours[cls], x=a["x"], y=a["y"], w=a["width"], h=a["height"], rx=a["rx"]
        )
        for cls, a in bars
    )
    return '<g transform="translate({:.4f} {:.4f}) scale({:.4f})">{}</g>'.format(tx, ty, scale, rects)


def icon_svg(colours, bars, size, mark_frac, corner_frac):
    """Square icon: ground (optionally rounded) with the mark at mark_frac of the width."""
    ground = '<rect width="{s}" height="{s}" rx="{r}" fill="{c}"/>'.format(
        s=size, r=size * corner_frac, c=colours["g"]
    )
    scale = size * mark_frac / MARK_W
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="{s}" height="{s}" viewBox="0 0 {s} {s}">{g}{m}</svg>'
        .format(s=size, g=ground, m=mark_group(colours, bars, scale, size / 2, size / 2))
    )


def og_html(colours, bars):
    mark = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="104" height="80" viewBox="0 0 104 80">{}</svg>'
        .format(mark_group(colours, bars, 4.0, 52, 40))
    )
    return """<!doctype html><html><head><meta charset="utf-8"><style>
@font-face {{ font-family: 'Schibsted Grotesk'; font-weight: 400 900;
  src: url('file://{font}') format('woff2'); }}
html, body {{ margin: 0; width: 1200px; height: 630px; background: {paper}; }}
body {{ box-sizing: border-box; padding: 72px 80px; font-family: 'Schibsted Grotesk', sans-serif;
  color: {ink}; display: flex; flex-direction: column; justify-content: space-between; }}
.brand {{ display: flex; align-items: center; gap: 20px; font-weight: 800; font-size: 72px;
  letter-spacing: -0.035em; line-height: 1; }}
.line {{ font-weight: 800; font-size: 68px; letter-spacing: -0.035em; line-height: 1.05; max-width: 1040px; }}
</style></head><body>
<div class="brand">{mark}<span>podskrift</span></div>
<div class="line">Get the transcript of any podcast episode, even on Spotify</div>
</body></html>""".format(font=FONT, paper=colours["g"], ink=colours["b"], mark=mark)


def find_chromium():
    env = os.environ.get("CHROMIUM")
    if env:
        return env
    pats = [
        "~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-mac*/chrome-headless-shell",
        "~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/*.app/Contents/MacOS/*",
        "~/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
        "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell",
    ]
    for p in pats:
        hits = sorted(glob.glob(os.path.expanduser(p)))
        if hits:
            return hits[-1]
    sys.exit("Chromium not found: set CHROMIUM=/path/to/chrome")


def shoot(chrome, html, width, height, out, transparent=False):
    """Render an HTML string to a PNG of exactly width x height."""
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "page.html")
        with open(src, "w", encoding="utf-8") as f:
            f.write(html)
        cmd = [
            chrome, "--headless", "--disable-gpu", "--hide-scrollbars", "--no-sandbox",
            "--force-device-scale-factor=1", "--allow-file-access-from-files",
            "--virtual-time-budget=3000",
            "--default-background-color={}".format("00000000" if transparent else "FFFFFFFF"),
            "--window-size={},{}".format(width, height), "--screenshot=" + out, "file://" + src,
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def icon_png(chrome, svg, size, out, transparent):
    html = '<!doctype html><html><body style="margin:0;background:transparent">{}</body></html>'.format(svg)
    shoot(chrome, html, size, size, out, transparent)


def write_ico(frames, out):
    """frames: list of (size, png bytes). PNG-in-ICO, readable by every modern browser."""
    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    entries, blobs = b"", b""
    for size, data in frames:
        entries += struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blobs += data
    with open(out, "wb") as f:
        f.write(header + entries + blobs)


def main():
    colours, bars = read_mark()
    chrome = find_chromium()
    out = lambda name: os.path.join(STATIC, name)

    # Rounded "any" icons: same geometry as favicon.svg (mark 26/32 wide, corner 8/32).
    for size in (192, 512):
        icon_png(chrome, icon_svg(colours, bars, size, 26 / 32, 8 / 32), size,
                 out("icon-{}.png".format(size)), transparent=True)
    # Maskable: full-bleed solid ground, mark inside the central safe zone (60% wide).
    icon_png(chrome, icon_svg(colours, bars, 512, 0.60, 0), 512, out("icon-maskable-512.png"), False)
    # iOS rounds the corners itself: opaque, full-bleed.
    icon_png(chrome, icon_svg(colours, bars, 180, 0.70, 0), 180, out("apple-touch-icon.png"), False)

    frames = []
    with tempfile.TemporaryDirectory() as tmp:
        for size in (16, 32, 48):
            p = os.path.join(tmp, "f{}.png".format(size))
            icon_png(chrome, icon_svg(colours, bars, size, 26 / 32, 8 / 32), size, p, True)
            frames.append((size, open(p, "rb").read()))
    write_ico(frames, out("favicon.ico"))

    shoot(chrome, og_html(colours, bars), 1200, 630, out("og-image.png"))

    try:  # flatten icon/og PNG colour modes the way the current files have them
        from PIL import Image
        for name, mode in (("og-image.png", "RGB"), ("apple-touch-icon.png", "RGB"),
                           ("icon-maskable-512.png", "RGB")):
            im = Image.open(out(name))
            if im.mode != mode:
                im.convert(mode).save(out(name), optimize=True)
    except ImportError:
        pass
    print("wrote brand assets to", STATIC)


if __name__ == "__main__":
    main()
