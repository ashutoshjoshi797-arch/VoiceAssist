"""Builds the website edition (online only) into site/ from the shared UI in web/.

    python packaging/web/build_site.py [out_dir]

The page is the same UI as the desktop and phone apps; web-engine.js answers its /api calls inside the
browser: VoiceAssist cloud accounts and the shared cloud AI (the product's Supabase project), history in this
browser. No offline mode. The Supabase URL and public (anon) key come from voiceassist/cloud.json or the
VOICEASSIST_SUPABASE_URL / VOICEASSIST_SUPABASE_KEY environment variables; no AI key is ever in the page.
"""
import json
import os
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
html = html.replace('<script src="app.js"></script>',
                    '<script src="cloud-config.js"></script>\n<script src="web-engine.js"></script>\n<script src="app.js"></script>')
cfg = json.loads((ROOT / "voiceassist" / "cloud.json").read_text(encoding="utf-8"))
cloud = {"url": (os.environ.get("VOICEASSIST_SUPABASE_URL") or cfg.get("supabase_url") or "").rstrip("/"),
         "key": os.environ.get("VOICEASSIST_SUPABASE_KEY") or cfg.get("supabase_anon_key") or "",
         "site_url": os.environ.get("VOICEASSIST_SITE_URL") or cfg.get("site_url") or ""}
(out / "cloud-config.js").write_text("window.VA_CLOUD = " + json.dumps(cloud) + ";\n", encoding="utf-8")
html = html.replace("<title>VoiceAssist</title>", "<title>VoiceAssist — talk to AI in your browser</title>")
# the website edition is online only: don't promise offline use on its pages
for old, new in [
    ("Your AI assistant,<br>online or offline.", "Your AI assistant,<br>right in your browser."),
    ("<li><b>Works without internet</b> — even in airplane mode.</li>",
     "<li><b>Nothing to install</b> — open the link and talk. For offline use, install the VoiceAssist app.</li>"),
    ("Talk to your AI — online or offline", "Talk to your AI"),
]:
    assert old in html, old
    html = html.replace(old, new)
js = out / "app.js"
js.write_text(js.read_text(encoding="utf-8").replace("Talk to your AI — online or offline", "Talk to your AI"), encoding="utf-8")
idx.write_text(html, encoding="utf-8")
(out / ".nojekyll").write_text("")
print("website built in", out)
