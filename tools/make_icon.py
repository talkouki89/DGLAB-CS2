"""產生程式圖示（assets/icon.ico / icon.png）。

設計：深色圓角底 + 霓虹閃電（電擊）+ 底部的心跳線（血量）。
用小畫家式的手繪座標，刻意做得粗略但辨識度高 —— 16x16 的時候
只有閃電看得出來，所以其他元素都只是陪襯。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 256
ASSETS = Path(__file__).resolve().parent.parent / "assets"

#: 底色漸層（上 -> 下）
BG_TOP = (18, 18, 30)
BG_BOTTOM = (54, 26, 82)
#: 閃電漸層
BOLT_TOP = (125, 240, 255)
BOLT_BOTTOM = (56, 170, 248)
#: 心跳線
PULSE = (140, 255, 220, 210)

BOLT = [
    (152, 24),
    (92, 140),
    (126, 140),
    (104, 232),
    (172, 108),
    (134, 108),
]

PULSE_POINTS = [
    (30, 202),
    (74, 202),
    (90, 178),
    (106, 226),
    (122, 166),
    (138, 202),
    (226, 202),
]


def vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    image = Image.new("RGBA", (size, size))
    draw = ImageDraw.Draw(image)
    for y in range(size):
        ratio = y / max(1, size - 1)
        color = tuple(
            int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3)
        ) + (255,)
        draw.line([(0, y), (size, y)], fill=color)
    return image


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=radius, fill=255
    )
    return mask


def build_icon(size: int = SIZE) -> Image.Image:
    base = vertical_gradient(size, BG_TOP, BG_BOTTOM)
    base.putalpha(rounded_mask(size, int(size * 0.22)))

    # 內側淡淡的亮邊，讓它看起來有質感
    edge = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle(
        (1, 1, size - 2, size - 2),
        radius=int(size * 0.22),
        outline=(120, 200, 255, 90),
        width=max(1, size // 96),
    )
    base = Image.alpha_composite(base, edge)

    scale = size / 256
    bolt = [(x * scale, y * scale) for x, y in BOLT]

    # 光暈：把閃電畫在另一層、模糊之後墊在下面
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(glow).polygon(bolt, fill=(90, 220, 255, 190))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=max(2, size * 0.055)))
    base = Image.alpha_composite(base, glow)
    base = Image.alpha_composite(base, glow)  # 疊兩次讓光暈更明顯

    # 閃電本體（用漸層遮色片做出上亮下深）
    bolt_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(bolt_layer).polygon(bolt, fill=(255, 255, 255, 255))
    gradient = vertical_gradient(size, BOLT_TOP, BOLT_BOTTOM)
    bolt_layer = Image.composite(gradient, Image.new("RGBA", (size, size), (0, 0, 0, 0)),
                                 bolt_layer.split()[3])
    base = Image.alpha_composite(base, bolt_layer)

    # 心跳線
    pulse = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(pulse).line(
        [(x * scale, y * scale) for x, y in PULSE_POINTS],
        fill=PULSE,
        width=max(1, int(7 * scale)),
        joint="curve",
    )
    base = Image.alpha_composite(base, pulse)
    return base


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    icon = build_icon(SIZE)
    png = ASSETS / "icon.png"
    icon.save(png)

    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    ico = ASSETS / "icon.ico"
    icon.save(ico, format="ICO", sizes=sizes)

    # 預覽各種尺寸（確認 16x16 還看得出來）
    preview = Image.new("RGBA", (16 + 24 + 32 + 48 + 64 + 20 * 5, 64), (24, 24, 34, 255))
    x = 10
    for side in (16, 24, 32, 48, 64):
        small = icon.resize((side, side), Image.LANCZOS)
        preview.alpha_composite(small, (x, (64 - side) // 2))
        x += side + 20
    preview.save(ASSETS / "icon-preview.png")

    print(f"  已產生 {png}  ({png.stat().st_size // 1024} KB)")
    print(f"  已產生 {ico}  ({ico.stat().st_size // 1024} KB，{len(sizes)} 種尺寸)")
    print(f"  已產生 {ASSETS / 'icon-preview.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
