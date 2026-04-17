"""Generate the desktop icon used by the launcher shortcut.

Run once with ``python assets/make_icon.py``. Produces ``assets/icon.ico``
containing 16/32/48/64/128/256-px frames. Uses only Pillow; no network.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


def _radial_gradient(size: int, inner: tuple[int, int, int], outer: tuple[int, int, int]) -> Image.Image:
    """Build a soft purple → magenta radial backdrop for the icon."""
    img = Image.new("RGB", (size, size), outer)
    pixels = img.load()
    cx = cy = size / 2
    max_dist = (cx ** 2 + cy ** 2) ** 0.5
    for y in range(size):
        for x in range(size):
            t = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 / max_dist
            t = min(1.0, t)
            r = int(inner[0] * (1 - t) + outer[0] * t)
            g = int(inner[1] * (1 - t) + outer[1] * t)
            b = int(inner[2] * (1 - t) + outer[2] * t)
            pixels[x, y] = (r, g, b)
    return img


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def _draw_folder(size: int) -> Image.Image:
    """Draw a stylized folder + camera lens centered on the canvas."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = size * 0.16
    folder_top = size * 0.34
    folder_bottom = size * 0.82
    tab_w = size * 0.32
    tab_h = size * 0.10
    tab_corner = size * 0.04
    folder_corner = size * 0.06

    # Folder tab
    draw.rounded_rectangle(
        (pad, folder_top - tab_h, pad + tab_w, folder_top + tab_corner),
        radius=tab_corner, fill=(255, 255, 255, 235),
    )
    # Folder body
    draw.rounded_rectangle(
        (pad, folder_top, size - pad, folder_bottom),
        radius=folder_corner, fill=(255, 255, 255, 245),
    )

    # Inner shadow strip across the top of the body for depth
    strip_h = size * 0.06
    strip = Image.new("RGBA", (int(size - pad * 2), int(strip_h)), (139, 92, 246, 60))
    img.paste(strip, (int(pad), int(folder_top)), strip)

    # Camera lens centered on the folder
    lens_cx = size / 2
    lens_cy = (folder_top + folder_bottom) / 2 + size * 0.02
    outer_r = size * 0.16
    mid_r = size * 0.12
    inner_r = size * 0.07
    glint_r = size * 0.025

    draw.ellipse(
        (lens_cx - outer_r, lens_cy - outer_r, lens_cx + outer_r, lens_cy + outer_r),
        fill=(124, 58, 237, 255),
    )
    draw.ellipse(
        (lens_cx - mid_r, lens_cy - mid_r, lens_cx + mid_r, lens_cy + mid_r),
        fill=(217, 70, 239, 255),
    )
    draw.ellipse(
        (lens_cx - inner_r, lens_cy - inner_r, lens_cx + inner_r, lens_cy + inner_r),
        fill=(15, 23, 42, 255),
    )
    draw.ellipse(
        (lens_cx - inner_r * 0.45 - glint_r, lens_cy - inner_r * 0.45 - glint_r,
         lens_cx - inner_r * 0.45 + glint_r, lens_cy - inner_r * 0.45 + glint_r),
        fill=(255, 255, 255, 220),
    )

    return img


def build_icon(out_path: Path, base: int = 512) -> None:
    bg = _radial_gradient(base, (167, 139, 250), (88, 28, 135)).convert("RGBA")
    mask = _rounded_mask(base, radius=int(base * 0.22))
    bg.putalpha(mask)

    # Soft outer shadow
    shadow = Image.new("RGBA", (base, base), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle(
        (8, 12, base - 8, base - 4), radius=int(base * 0.22),
        fill=(0, 0, 0, 110),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=base * 0.025))

    canvas = Image.new("RGBA", (base, base), (0, 0, 0, 0))
    canvas.alpha_composite(shadow)
    canvas.alpha_composite(bg)
    canvas.alpha_composite(_draw_folder(base))

    sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
    canvas.save(out_path, format="ICO", sizes=sizes)
    print(f"Wrote {out_path} ({len(sizes)} sizes)")


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    build_icon(here / "icon.ico")
