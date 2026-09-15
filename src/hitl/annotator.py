"""
hitl/annotator.py
------------------
Annotates failure screenshots with a red error banner so the operator
immediately understands what went wrong and at which step.
"""

from __future__ import annotations
import logging
import os

logger = logging.getLogger(__name__)


def annotate_screenshot(
    raw_path: str,
    step_id: str,
    reason: str,
    current_url: str,
    out_path: str = None,
) -> str:
    """
    Draw a red error banner on raw_path and save to out_path.
    Returns out_path on success, raw_path if Pillow is unavailable.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.warning("[annotator] Pillow not installed — skipping annotation")
        return raw_path

    out_path = out_path or raw_path

    try:
        img = Image.open(raw_path).convert("RGBA")
        W, H = img.size
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        BANNER_H = 120
        BORDER   = 5

        # Semi-transparent dark banner at top
        draw.rectangle([(0, 0), (W, BANNER_H)], fill=(10, 5, 5, 220))

        # Red border around entire image
        for t in range(BORDER):
            draw.rectangle([(t, t), (W - 1 - t, H - 1 - t)], outline=(210, 35, 35, 200))

        font_title = _load_font(15, bold=True)
        font_body  = _load_font(12, bold=False)

        # Red alert strip
        draw.rectangle([(0, 0), (W, 30)], fill=(185, 28, 28, 245))
        draw.text(
            (12, 7),
            "[!!] AUTOMATION STUCK — HUMAN INTERVENTION REQUIRED",
            font=font_title,
            fill=(255, 220, 220, 255),
        )

        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        draw.text((12, 38), f"Step: {step_id}    |    {ts}", font=font_body, fill=(180, 180, 180, 220))

        url_display = current_url[:115] + "..." if len(current_url) > 115 else current_url
        draw.text((12, 60), f"URL:  {url_display}", font=font_body, fill=(140, 180, 255, 230))

        reason_line = reason.split("\n")[0][:200]
        draw.text((12, 82), f"Why:  {reason_line}", font=font_body, fill=(255, 160, 80, 240))

        combined = Image.alpha_composite(img, overlay)
        combined.convert("RGB").save(out_path, "PNG")
        logger.info(f"[annotator] Annotated screenshot saved: {out_path}")
        return out_path

    except Exception as e:
        logger.warning(f"[annotator] Annotation failed: {e}")
        return raw_path


def _load_font(size: int, bold: bool = False):
    from PIL import ImageFont
    candidates = [
        "C:/Windows/Fonts/consolab.ttf" if bold else "C:/Windows/Fonts/consola.ttf",
        "C:/Windows/Fonts/arialbd.ttf"  if bold else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf" if bold
            else "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()
