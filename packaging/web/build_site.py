"""Builds the website edition (online only) into site/ from the shared UI in web/.

    python packaging/web/build_site.py [out_dir]

The page is the same UI as the desktop and phone apps; web-engine.js answers its /api calls inside the
browser (accounts, history and keys in this browser; answers from Claude / OpenAI / Gemini). No offline mode.
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "site"
if out.exists():
    shutil.rmtree(out)
shutil.copytree(ROOT / "web", out)
idx = out / "index.html"
html = idx.read_text(encoding="utf-8")
assert '<script src="app.js"></script>' in html
html = html.replace('<script src="app.js"></script>', '<script src="web-engine.js"></script>\n<script src="app.js"></script>')
html = html.replace("<title>VoiceAssist</title>", "<title>VoiceAssist — talk to AI in your browser</title>")
idx.write_text(html, encoding="utf-8")
(out / ".nojekyll").write_text("")
print("website built in", out)
