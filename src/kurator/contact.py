"""Контакт-листы: превью-сетки для ревью глазами."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .imaging import BadImage, open_rgb

THUMB = 260
LABEL_H = 34
PAD = 10
COLS = 4
BG = (24, 24, 27)
FG = (228, 228, 231)
ACCENT = (250, 204, 21)
MUTED = (161, 161, 170)


def _font(size: int) -> ImageFont.ImageFont:
    for path in (
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ):
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _short(path: str, limit: int = 30) -> str:
    name = Path(path).name
    return name if len(name) <= limit else name[: limit - 1] + "…"


def contact_sheet(
    root: Path,
    rel_paths: list[str],
    out_path: Path,
    title: str = "",
    keepers: set[str] | None = None,
    cols: int = COLS,
) -> Path | None:
    thumbs: list[tuple[str, Image.Image]] = []
    for rel in rel_paths:
        try:
            img = open_rgb(root / rel)
        except BadImage:
            continue
        img.thumbnail((THUMB, THUMB), Image.LANCZOS)
        thumbs.append((rel, img))
    if not thumbs:
        return None

    cols = max(1, min(cols, len(thumbs)))
    rows = (len(thumbs) + cols - 1) // cols
    title_h = 34 if title else 0
    sheet_w = PAD + cols * (THUMB + PAD)
    sheet_h = title_h + PAD + rows * (THUMB + LABEL_H + PAD)

    sheet = Image.new("RGB", (sheet_w, sheet_h), BG)
    draw = ImageDraw.Draw(sheet)
    label_font = _font(15)
    title_font = _font(17)

    if title:
        draw.text((PAD, 9), title, fill=ACCENT, font=title_font)

    for i, (rel, thumb) in enumerate(thumbs):
        col = i % cols
        row = i // cols
        x = PAD + col * (THUMB + PAD)
        y = title_h + PAD + row * (THUMB + LABEL_H + PAD)
        is_keeper = bool(keepers and rel in keepers)
        frame = ACCENT if is_keeper else (70, 70, 78)
        draw.rectangle([x - 1, y - 1, x + THUMB, y + THUMB], outline=frame, width=2 if is_keeper else 1)
        sheet.paste(thumb, (x + (THUMB - thumb.width) // 2, y + (THUMB - thumb.height) // 2))
        name_color = FG if is_keeper else MUTED
        draw.text((x + 2, y + THUMB + 5), _short(rel), fill=name_color, font=label_font)
        tag = "оставляю" if is_keeper else "кандидат"
        tag_color = ACCENT if is_keeper else (120, 120, 128)
        draw.text((x + 2, y + THUMB + 19), tag, fill=tag_color, font=_font(12))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, format="JPEG", quality=82, optimize=True)
    return out_path