"""Generates the app icons from packaging/windows/voiceassist.png (run by CI before building):
Windows .ico (16-256 px) and the Android launcher PNGs (mdpi..xxxhdpi)."""
from pathlib import Path
from PIL import Image

root = Path(__file__).resolve().parents[1]
src = Image.open(root / "packaging/windows/voiceassist.png").convert("RGBA")
src.save(root / "packaging/windows/voiceassist.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
for d, px in {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}.items():
    out = root / f"android/app/src/main/res/mipmap-{d}/ic_launcher.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    src.resize((px, px), Image.LANCZOS).save(out)
print("icons generated")
