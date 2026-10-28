"""
tools/board_hostile_test.py - drives the REAL runtime/study_board.html in headless Chromium (Playwright).

Run:  python tools/board_hostile_test.py [path/to/study_board.html]      (exit code != 0 on any FAIL)

Checks (master prompt PART 7.2 gate 3, WP9):
  * hostile SVG payloads: window.__pwned stays undefined, nothing leaves the page, the board stays usable;
  * filters (feGaussianBlur, feDropShadow ...) survive and render;
  * reveal="all" leaves the board scrolled to the top (the heading is not cut off);
  * generated figures get the "AI-generated schematic" chip, curated ones do not;
  * works with runtime/vendor/ absent (KaTeX/Plotly are not needed for diagrams).
Dev tool only - not loaded by the app.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
BOARD = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "runtime" / "study_board.html"
NS = 'xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 660"'


def svg(inner: str) -> str:
    return f"<svg {NS}>{inner}</svg>"


HOSTILE = {
    "script tag": svg('<script>window.__pwned=1</script><rect width="9" height="9"/>'),
    "onload handler": svg('<rect width="9" height="9" onload="window.__pwned=1"/>'),
    "onclick handler": svg('<rect id="r" width="900" height="600" onclick="window.__pwned=1"/>'),
    "foreignObject img onerror": svg('<foreignObject width="50" height="50"><img src="x" onerror="window.__pwned=1"/></foreignObject>'),
    "image http href": svg('<image href="http://evil.example/p.png" width="9" height="9"/>'),
    "anchor javascript href": svg('<a xlink:href="javascript:window.__pwned=1"><rect width="9" height="9"/></a>'),
    "use http href": svg('<use href="http://evil.example/x.svg#a"/>'),
    "style url(http)": svg('<rect width="9" height="9" style="fill:url(http://evil.example/p)"/>'),
    "style @import": svg('<style>@import url(http://evil.example/x.css);</style><rect width="9" height="9"/>'),
    "feImage remote": svg('<filter id="f"><feImage href="http://evil.example/i.png"/></filter><rect width="9" height="9" filter="url(#f)"/>'),
    "trailing img after </svg>": svg('<rect width="9" height="9"/>') + '<img src="x" onerror="window.__pwned=1">',
    "DOCTYPE/ENTITY bomb": '<!DOCTYPE svg [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
                           + svg('<text x="5" y="20">&b;&b;&b;</text>'),
    "5 MB svg": svg('<rect width="1" height="1"/>' * 200_000),
}

GOOD_FILTER = svg(
    '<defs><filter id="sh"><feGaussianBlur stdDeviation="4"/></filter>'
    '<filter id="ds"><feDropShadow dx="2" dy="2" stdDeviation="2"/></filter></defs>'
    '<rect width="1000" height="660" fill="#07111D"/>'
    '<ellipse cx="500" cy="580" rx="300" ry="30" fill="#000" opacity="0.4" filter="url(#sh)"/>'
    '<circle cx="500" cy="300" r="150" fill="#38BDF8" filter="url(#ds)"/>')

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, why: str = "") -> None:
    results.append((name, ok, why))
    print(("PASS  " if ok else "FAIL  ") + name + (f"  -> {why}" if why and not ok else ""))


def render(page, markdown: str, reveal: str = "all") -> None:
    page.evaluate("p => ops.render(p)", {"title": "T", "markdown": markdown, "subject": "", "reveal": reveal})
    page.wait_for_timeout(350)


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    outgoing: list[str] = []

    def on_request(req):
        if not req.url.startswith(("file:", "data:", "blob:", "about:")):
            outgoing.append(req.url)

    ctx.route("**/*", lambda route: route.abort() if not route.request.url.startswith(("file:", "data:", "blob:", "about:")) else route.continue_())
    page = ctx.new_page()
    page.on("request", on_request)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BOARD.as_uri())
    page.wait_for_timeout(800)
    record("board loads and exposes ops.render", bool(page.evaluate("typeof ops !== 'undefined' && typeof ops.render === 'function'")))

    for name, payload in HOSTILE.items():
        page.evaluate("delete window.__pwned")
        outgoing.clear()
        render(page, f"## Fig\n```svg\n{payload}\n```")
        try:
            page.click("#board svg rect", timeout=300, force=True)       # fires an injected onclick if one survived
        except Exception:
            pass
        page.wait_for_timeout(250)
        pwned = page.evaluate("window.__pwned")
        html = page.evaluate("document.getElementById('board').innerHTML").lower()
        leaked = [u for u in outgoing if "evil.example" in u]
        bad_dom = [t for t in ("<script", "onerror", "onload", "onclick", "foreignobject", "<image", "evil.example", "javascript:") if t in html]
        ok = pwned is None and not outgoing and not bad_dom
        record(f"hostile: {name}", ok, f"pwned={pwned!r} outgoing={outgoing[:2]} leaked={leaked[:1]} dom={bad_dom}")

    # board still usable after the whole suite
    render(page, "## After\n```svg\n" + svg('<rect width="50" height="50" fill="#38BDF8"/>') + "\n```")
    record("board still usable after hostile suite", page.evaluate("document.querySelectorAll('#board svg').length") == 1)

    # filters survive and render
    render(page, "## Filters\n```svg\n" + GOOD_FILTER + "\n```")
    kept = page.evaluate("""() => ({
        blur: document.querySelectorAll('#board feGaussianBlur').length,
        shadow: document.querySelectorAll('#board feDropShadow').length,
        box: (() => { const c = document.querySelector('#board svg circle'); const r = c && c.getBoundingClientRect(); return r ? [r.width, r.height] : [0, 0]; })()
    })""")
    record("filters survive the sanitizer", kept["blur"] == 1 and kept["shadow"] == 1, str(kept))
    record("filtered shapes render with real size", kept["box"][0] > 20 and kept["box"][1] > 20, str(kept))

    # width: diagrams may now use up to 1000 px
    maxw = page.evaluate("getComputedStyle(document.querySelector('#board .svg-diagram-box svg')).maxWidth")
    record("diagram max-width is 1000px", maxw == "1000px", maxw)

    # reveal="all": heading visible, not scrolled to the last card
    long_md = "".join(f"## Part {i}\n" + ("Line of text.\n\n" * 18) for i in range(1, 5))
    long_md = "## Figure\n```svg\n" + svg('<rect width="1000" height="660" fill="#07111D"/>') + "\n```\n\n" + long_md
    render(page, long_md, reveal="all")
    page.wait_for_timeout(900)                                            # a smooth scroll would still be moving
    top = page.evaluate("document.getElementById('board').scrollTop")
    record("reveal=all leaves scrollTop = 0", top == 0, f"scrollTop={top}")

    # generated chip
    gen_svg = svg('<rect width="9" height="9"/>').replace("<svg ", '<svg data-jv-generated="ai" ', 1)
    render(page, "## G\n```svg\n" + gen_svg + "\n```")
    chip = page.evaluate("document.querySelectorAll('#board .gen-chip').length")
    record("generated figure shows the AI chip", chip == 1, str(chip))
    render(page, "## C\n```svg\n" + svg('<rect width="9" height="9"/>') + "\n```")
    chip2 = page.evaluate("document.querySelectorAll('#board .gen-chip').length")
    record("curated figure shows no chip", chip2 == 0, str(chip2))

    # the real curated template still renders (rich SVG, gradients, filters)
    tpl = (ROOT / "diagrams" / "agaricus_mushroom.svg")
    if tpl.exists():
        render(page, "## Mushroom\n```svg\n" + tpl.read_text(encoding="utf-8") + "\n```")
        n = page.evaluate("document.querySelectorAll('#board svg *').length")
        record("curated mushroom template renders", n > 300, f"{n} elements")

    record("no uncaught page errors", not errors, "; ".join(errors[:2]))
    browser.close()

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed  (board: {BOARD})")
sys.exit(1 if failed else 0)
