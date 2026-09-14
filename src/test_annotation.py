"""
Test annotation locally - run from src/
python test_annotation.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from PIL import Image, ImageDraw, ImageFont

# Check what fonts are available on Windows
import glob

windows_fonts = [
    "C:/Windows/Fonts/consola.ttf",      # Consolas
    "C:/Windows/Fonts/consolab.ttf",     # Consolas Bold
    "C:/Windows/Fonts/arial.ttf",        # Arial
    "C:/Windows/Fonts/arialbd.ttf",      # Arial Bold
    "C:/Windows/Fonts/cour.ttf",         # Courier New
    "C:/Windows/Fonts/courbd.ttf",       # Courier New Bold
    "C:/Windows/Fonts/segoeui.ttf",      # Segoe UI
    "C:/Windows/Fonts/calibri.ttf",      # Calibri
]

print("Checking Windows fonts:")
found = []
for f in windows_fonts:
    exists = os.path.exists(f)
    print(f"  {'OK' if exists else '--'}  {f}")
    if exists:
        found.append(f)

print(f"\nFound {len(found)} fonts")

# Try loading each one
print("\nTrying to load fonts:")
for f in found:
    try:
        font = ImageFont.truetype(f, 14)
        print(f"  OK  {f}")
    except Exception as e:
        print(f"  FAIL {f}: {e}")

# Create a test annotation
print("\nCreating test annotation...")
img = Image.new("RGB", (800, 200), color=(30, 30, 30))
draw = ImageDraw.Draw(img)

# Try to load best available font
test_font = None
for f in ["C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/cour.ttf"]:
    try:
        test_font = ImageFont.truetype(f, 16)
        print(f"Using font: {f}")
        break
    except:
        pass

if test_font is None:
    test_font = ImageFont.load_default()
    print("Using default font (no TTF found)")

draw.rectangle([(0,0),(800,40)], fill=(180, 30, 30))
draw.text((10, 10), "TEST: AUTOMATION STUCK - HUMAN INTERVENTION REQUIRED", font=test_font, fill=(255,255,255))
draw.text((10, 55), "Step: s3  |  2026-09-15 10:00:00 UTC", font=test_font, fill=(180,180,180))
draw.text((10, 80), "URL: http://localhost:8080/search", font=test_font, fill=(140,180,255))
draw.text((10, 105), "Why: All 2 locators exhausted - button not found", font=test_font, fill=(255,160,100))

out = os.path.join(os.path.dirname(__file__), "..", "evidence", "screenshots", "annotation_test.png")
os.makedirs(os.path.dirname(out), exist_ok=True)
img.save(out)
print(f"\nSaved test image: {out}")
print("Open it and check if text is visible - if yes, annotation is working")