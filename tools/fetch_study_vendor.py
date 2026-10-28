"""
Downloads offline web vendor libraries required by runtime/study_board.html:
- KaTeX (JS, CSS, auto-render, fonts)
- Tailwind JS
- Plotly JS
"""
import sys
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
VENDOR_DIR = BASE_DIR / "runtime" / "vendor"

ASSETS = [
    # Tailwind
    ("https://cdn.tailwindcss.com", VENDOR_DIR / "tailwind" / "tailwind.js"),
    # KaTeX JS & CSS
    ("https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js", VENDOR_DIR / "katex" / "katex.min.js"),
    ("https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css", VENDOR_DIR / "katex" / "katex.min.css"),
    ("https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js", VENDOR_DIR / "katex" / "auto-render.min.js"),
    # Plotly
    ("https://cdn.plot.ly/plotly-2.35.2.min.js", VENDOR_DIR / "plotly" / "plotly.min.js"),
]

# KaTeX core fonts for proper mathematical symbol rendering
FONTS = [
    "KaTeX_AMS-Regular.woff2", "KaTeX_Caligraphic-Bold.woff2", "KaTeX_Caligraphic-Regular.woff2",
    "KaTeX_Fraktur-Bold.woff2", "KaTeX_Fraktur-Regular.woff2", "KaTeX_Main-Bold.woff2",
    "KaTeX_Main-BoldItalic.woff2", "KaTeX_Main-Italic.woff2", "KaTeX_Main-Regular.woff2",
    "KaTeX_Math-BoldItalic.woff2", "KaTeX_Math-Italic.woff2", "KaTeX_SansSerif-Bold.woff2",
    "KaTeX_SansSerif-Italic.woff2", "KaTeX_SansSerif-Regular.woff2", "KaTeX_Script-Regular.woff2",
    "KaTeX_Size1-Regular.woff2", "KaTeX_Size2-Regular.woff2", "KaTeX_Size3-Regular.woff2",
    "KaTeX_Size4-Regular.woff2", "KaTeX_Typewriter-Regular.woff2"
]

for font in FONTS:
    url = f"https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/fonts/{font}"
    ASSETS.append((url, VENDOR_DIR / "katex" / "fonts" / font))

def download_assets():
    print("⏳ Downloading Study Board vendor libraries...")
    headers = {"User-Agent": "Mozilla/5.0"}
    
    for url, dest in ASSETS:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size > 0:
            print(f"✓ Already present: {dest.name}")
            continue
        try:
            print(f"⬇ Downloading: {dest.name} ...")
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as f:
                f.write(resp.read())
            print(f"✓ Saved: {dest.name}")
        except Exception as e:
            print(f"❌ Failed to download {url}: {e}")

    print("\n✅ All vendor libraries downloaded successfully!")

if __name__ == "__main__":
    download_assets()