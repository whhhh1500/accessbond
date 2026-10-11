#!/usr/bin/env python3
"""Write the demo pages used by the v0.2 fix-warranty live run.

usage: python3 scripts/warranty_demo_pages.py <hours|catering> <broken|fixed|relapsed> [hunter address]

  broken    the starting page: no lang, empty title, images without alt, an empty link
  fixed     the hunter's fix (optionally naming the hunter in <meta name="accessbond:hunter">)
  relapsed  a later "theme update" adds new product photos without alt text: the most common
            way a paid accessibility fix silently regresses after the payout

The page is written to frontend/public/demo-site/ and docs/demo-site/ (GitHub Pages).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = {
    "hours": ("Bluebird Bakery opening hours", "Opening hours", ["croissant.svg"]),
    "catering": ("Bluebird Bakery catering", "Catering orders", ["sourdough.svg"]),
}


def page(name: str, state: str, hunter: str = "") -> str:
    title, h1, imgs = PAGES[name]
    fixed = state in ("fixed", "relapsed")
    lang = ' lang="en"' if fixed else ""
    meta = f'\n  <meta name="accessbond:hunter" content="{hunter}">' if fixed and hunter else ""
    imgs_html = "\n".join(
        f'  <img src="{src}" width="120"' + (f' alt="{src.split(".")[0].capitalize()} loaf"' if fixed else "") + ">"
        for src in imgs
    )
    if state == "relapsed":
        imgs_html += '\n  <!-- theme update: new seasonal photos, uploaded without alt text -->\n' \
                     '  <img src="croissant.svg" width="80">\n  <img src="sourdough.svg" width="80">'
    link = '<a href="https://example.com/contact">Contact the bakery</a>' if fixed else '<a href="https://example.com/contact"></a>'
    return f"""<!doctype html>
<html{lang}>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">{meta}
  <title>{title if fixed else ""}</title>
  <style>body{{font-family:sans-serif;max-width:720px;margin:2rem auto;padding:0 1rem}}</style>
</head>
<body>
  <h1>{h1}</h1>
{imgs_html}
  <p>Mon-Sat 7:00-18:00. Order 48 hours ahead for events.</p>
  {link}
</body>
</html>
"""


if __name__ == "__main__":
    name, state = sys.argv[1], sys.argv[2]
    hunter = sys.argv[3].lower() if len(sys.argv) > 3 else ""
    html = page(name, state, hunter)
    for d in ("frontend/public/demo-site", "docs/demo-site"):
        (ROOT / d / f"{name}.html").write_text(html)
    print(f"{name}.html -> {state}")
