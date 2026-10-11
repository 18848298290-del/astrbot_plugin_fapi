from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "assets" / "slot_machine.gif"
WIDTH = 480
HEIGHT = 270
SYMBOLS = ("7", "BAR", "GEM", "STAR", "BONUS")
COLORS = ("#f4c95d", "#f15b5a", "#5ed3a7", "#62a8ea", "#f08ac0")


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(Path("C:/Windows/Fonts") / name), size)


def centered(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str,
             text_font: ImageFont.FreeTypeFont, fill: str) -> None:
    left, top, right, bottom = box
    bounds = draw.textbbox((0, 0), text, font=text_font)
    x = left + ((right - left) - (bounds[2] - bounds[0])) / 2
    y = top + ((bottom - top) - (bounds[3] - bounds[1])) / 2 - bounds[1]
    draw.text((x, y), text, font=text_font, fill=fill)


def make_frame(frame_index: int) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#071d18")
    draw = ImageDraw.Draw(image)
    title_font = font("arialbd.ttf", 28)
    symbol_font = font("arialbd.ttf", 25)
    small_font = font("arial.ttf", 14)

    for x in range(0, WIDTH, 24):
        draw.line((x, 0, x, HEIGHT), fill="#0d3028", width=1)
    for y in range(0, HEIGHT, 24):
        draw.line((0, y, WIDTH, y), fill="#0d3028", width=1)

    draw.rounded_rectangle((38, 28, 442, 242), radius=18, fill="#102a24",
                           outline="#f4c95d", width=4)
    draw.rounded_rectangle((58, 78, 422, 191), radius=10, fill="#061410",
                           outline="#326e5d", width=3)
    centered(draw, (40, 31, 440, 71), "FAPI SLOT", title_font, "#f4c95d")

    reel_lefts = (72, 190, 308)
    for reel_index, left in enumerate(reel_lefts):
        box = (left, 91, left + 100, 178)
        draw.rounded_rectangle(box, radius=8, fill="#ecf3ef", outline="#9db5ad", width=2)
        symbol_index = (frame_index * (reel_index + 2) + reel_index) % len(SYMBOLS)
        symbol = SYMBOLS[symbol_index]
        color = COLORS[symbol_index]
        centered(draw, box, symbol, symbol_font, color)
        highlight_y = 98 + ((frame_index * 9 + reel_index * 17) % 65)
        draw.line((left + 7, highlight_y, left + 93, highlight_y), fill="#ffffff", width=2)

    dot_count = (frame_index % 3) + 1
    centered(draw, (40, 199, 440, 229), "SPINNING" + "." * dot_count,
             small_font, "#8fe3c5")
    return image


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    frames = [make_frame(index) for index in range(15)]
    frames[0].save(
        OUTPUT,
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
        optimize=True,
        disposal=2,
    )
    print(OUTPUT)


if __name__ == "__main__":
    main()
