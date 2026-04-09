"""Generate agt_icon.ico — dark slate background with bold white "AGT" text.

Run once: python launcher/_make_icon.py
Output: launcher/agt_icon.ico (32x32 + 256x256 packed)
"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

OUTPUT = Path(__file__).parent / "agt_icon.ico"

SIZES = [32, 256]
BG_COLOR = (13, 17, 23)  # #0d1117 (GitHub dark)
TEXT_COLOR = (255, 255, 255)


def make_icon_image(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), BG_COLOR + (255,))
    draw = ImageDraw.Draw(img)

    # Use a built-in font, scaled to fit
    font_size = int(size * 0.35)
    try:
        font = ImageFont.truetype("arial.ttf", font_size)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("arialbd.ttf", font_size)
        except (OSError, IOError):
            font = ImageFont.load_default()

    text = "AGT"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = (size - tw) // 2
    y = (size - th) // 2 - bbox[1]  # adjust for font ascent
    draw.text((x, y), text, fill=TEXT_COLOR, font=font)

    return img


def main():
    images = [make_icon_image(s) for s in SIZES]
    images[0].save(
        str(OUTPUT),
        format="ICO",
        sizes=[(s, s) for s in SIZES],
        append_images=images[1:],
    )
    print(f"Generated: {OUTPUT} ({OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
