from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "assets" / "app-icon.ico"
PNG_OUTPUT = ROOT / "assets" / "app-icon.png"


def main() -> None:
    size = 256
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((12, 12, 244, 244), radius=58, fill="#F7FAF7", outline="#A9B5AC", width=8)
    draw.arc((61, 61, 195, 195), start=-74, end=250, fill="#1C7649", width=18)
    draw.line((128, 128, 128, 82), fill="#202521", width=15)
    draw.line((128, 128, 166, 148), fill="#202521", width=15)
    draw.ellipse((119, 119, 137, 137), fill="#202521")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(PNG_OUTPUT)
    image.save(OUTPUT, sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    main()
