"""Генератор синтетического фотобанка для проверки пайплайна kurator.

Намеренно грязный набор: точные дубликаты, near-dupes с разным качеством
JPEG и ресайзом, burst-серии с суффиксами (N), разные по виду сцены и мусор
(не-изображения, скрытые файлы, видео-плейсхолдер, EXIF-файлы без метаданных).

    python scripts/make_testset.py /tmp/kurator-test 180
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import random
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

W, H = 900, 600

PALETTE = [
    (198, 63, 74),
    (72, 129, 165),
    (108, 168, 118),
    (211, 176, 84),
    (147, 106, 178),
    (86, 82, 74),
    (222, 222, 218),
    (54, 58, 66),
    (18, 22, 30),
    (240, 240, 232),
]


def _rect(draw, lrng, crng, palette, alpha=(110, 235)):
    x0 = lrng.randint(-80, W)
    y0 = lrng.randint(-80, H)
    w = lrng.randint(40, 320)
    h = lrng.randint(35, 280)
    fill = (*palette[crng.randrange(len(palette))], crng.randint(*alpha))
    if lrng.random() < 0.3:
        draw.ellipse([x0, y0, x0 + w, y0 + h], fill=fill)
    else:
        draw.rectangle([x0, y0, x0 + w, y0 + h], fill=fill)


def scene_portrait(lrng, crng, palette):
    img = Image.new("RGB", (W, H), palette[crng.randrange(len(palette))])
    draw = ImageDraw.Draw(img, "RGBA")
    cx = lrng.randint(260, W - 260)
    cy = lrng.randint(150, 260)
    head = lrng.randint(90, 150)
    skin = crng.choice([(232, 205, 175), (196, 154, 120), (140, 100, 72), (86, 62, 46)])
    cloth = palette[crng.randrange(len(palette))]
    hair = crng.choice([(38, 30, 26), (92, 66, 40), (140, 120, 96), (20, 20, 24), (200, 190, 180)])
    draw.ellipse([cx - 200, cy + head, cx + 200, H + 80], fill=cloth)
    draw.ellipse([cx - head, cy, cx + head, cy + head * 2], fill=skin)
    draw.ellipse([cx - head - 14, cy - 14, cx + head + 14, cy + head * 1.5], fill=hair)
    eye_dx = head // 2
    draw.ellipse([cx - eye_dx - 26, cy + head // 2, cx - eye_dx + 6, cy + head // 2 + 26], fill=(25, 25, 30))
    draw.ellipse([cx + eye_dx - 6, cy + head // 2, cx + eye_dx + 26, cy + head // 2 + 26], fill=(25, 25, 30))
    draw.arc([cx - 60, cy + head, cx + 60, cy + head + 70], 20, 160, fill=(120, 55, 55), width=7)
    return img


def scene_landscape(lrng, crng, palette):
    img = Image.new("RGB", (W, H), palette[crng.randrange(len(palette))])
    draw = ImageDraw.Draw(img, "RGBA")
    peaks = lrng.randint(2, 6)
    for i in range(peaks):
        x = lrng.randint(-120, W)
        peak = lrng.randint(30, 380)
        width = lrng.randint(120, 460)
        draw.polygon(
            [(x, H), (x + width // 2, peak), (x + width, H)],
            fill=(crng.randint(30, 140), crng.randint(60, 160), crng.randint(60, 180)),
        )
    ground = lrng.randint(20, 200)
    draw.rectangle([0, H - ground, W, H],
                   fill=(crng.randint(20, 90), crng.randint(50, 120), crng.randint(30, 90)))
    for _ in range(lrng.randint(0, 10)):
        x = lrng.randint(0, W)
        top = H - ground - lrng.randint(20, 130)
        draw.rectangle([x, top, x + lrng.randint(4, 14), H - ground], fill=(20, 45, 26))
    return img


def scene_screenshot(lrng, crng, palette):
    img = Image.new("RGB", (W, H), crng.choice([(245, 246, 248), (18, 18, 22), (250, 250, 248)]))
    draw = ImageDraw.Draw(img)
    accent = palette[crng.randrange(len(palette))]
    dark = img.getpixel((0, 0))[0] < 128
    fg = (60, 64, 72) if dark else (205, 210, 218)
    chip = (95, 100, 112) if dark else (190, 198, 210)
    draw.rectangle([0, 0, W, lrng.choice([40, 54, 64])], fill=accent if not dark else (60, 64, 72))
    rows = lrng.randint(4, 13)
    y = lrng.randint(70, 110)
    for _ in range(rows):
        if y + 40 > H - 40:
            break
        draw.rounded_rectangle([40, y, 70, y + 30], radius=6, fill=chip)
        draw.rectangle([90, y + 6, max(100, W - 60 - lrng.randint(0, 500)), y + 16], fill=fg)
        y += lrng.randint(38, 62)
    draw.rectangle([40, H - 110, 40 + lrng.randint(140, 340), H - 60], fill=accent)
    return img


def scene_document(lrng, crng, palette):
    img = Image.new("RGB", (W, H), crng.choice([(252, 250, 244), (255, 255, 255), (240, 238, 232)]))
    draw = ImageDraw.Draw(img)
    margin = lrng.randint(45, 95)
    draw.rectangle([margin, lrng.randint(20, 40), margin + lrng.randint(180, 420), 44], fill=(40, 42, 48))
    y = lrng.randint(70, 110)
    while y + 12 < H - 40:
        draw.rectangle([margin, y, W - margin - lrng.randint(0, 180), y + 7], fill=(80, 82, 88))
        y += lrng.randint(18, 28)
    return img


def scene_cat(lrng, crng, palette):
    img = Image.new("RGB", (W, H), palette[crng.randrange(len(palette))])
    draw = ImageDraw.Draw(img, "RGBA")
    coat = crng.choice([(58, 52, 48), (122, 104, 84), (36, 38, 42), (188, 176, 160), (214, 208, 196)])
    cx = lrng.randint(240, W - 240)
    cy = lrng.randint(200, 320)
    scale = lrng.uniform(0.65, 1.35)
    body_w = int(240 * scale)
    head = int(115 * scale)
    draw.ellipse([cx - body_w, cy, cx + body_w, H + 90], fill=coat)
    draw.ellipse([cx - head, cy - head * 1.3, cx + head, cy + head * 0.7], fill=coat)
    ear = head // 2
    draw.polygon([(cx - head + 10, cy - head), (cx - head + ear, cy - head - ear), (cx, cy - head + 20)], fill=coat)
    draw.polygon([(cx + head - 10, cy - head), (cx + head - ear, cy - head - ear), (cx, cy - head + 20)], fill=coat)
    eye_y = cy - head * 0.45
    for sign in (-1, 1):
        ex = cx + sign * head // 2
        draw.ellipse([ex - 26, eye_y, ex + 26, eye_y + 26], fill=(238, 212, 88))
        draw.ellipse([ex - 9, eye_y + 6, ex + 9, eye_y + 26], fill=(18, 18, 18))
    return img


def scene_nature(lrng, crng, palette):
    img = Image.new("RGB", (W, H), palette[crng.randrange(len(palette))])
    draw = ImageDraw.Draw(img, "RGBA")
    for _ in range(lrng.randint(8, 26)):
        _rect(draw, lrng, crng, palette, alpha=(70, 200))
    for _ in range(lrng.randint(3, 12)):
        _rect(draw, lrng, crng, palette, alpha=(180, 245))
    return img


def scene_food(lrng, crng, palette):
    img = Image.new("RGB", (W, H), (crng.randint(10, 240), crng.randint(10, 240), crng.randint(10, 240)))
    draw = ImageDraw.Draw(img, "RGBA")
    px, py = lrng.randint(220, W - 220), lrng.randint(200, H - 200)
    pr = lrng.randint(110, 200)
    draw.ellipse([px - pr, py - pr, px + pr, py + pr], fill=(240, 238, 232))
    for _ in range(lrng.randint(5, 16)):
        a = lrng.uniform(0, math.tau)
        r = lrng.uniform(0, pr * 0.75)
        rr = lrng.randint(14, 48)
        x = px + math.cos(a) * r
        y = py + math.sin(a) * r
        draw.ellipse([x - rr, y - rr, x + rr, y + rr], fill=palette[crng.randrange(len(palette))])
    return img


def scene_indoor(lrng, crng, palette):
    img = Image.new("RGB", (W, H), palette[crng.randrange(len(palette))])
    draw = ImageDraw.Draw(img, "RGBA")
    floor = lrng.randint(120, 340)
    draw.polygon([(0, floor), (W, floor - lrng.randint(-60, 60)), (W, H), (0, H)],
                 fill=(crng.randint(60, 170), crng.randint(50, 150), crng.randint(40, 130)))
    for _ in range(lrng.randint(1, 4)):
        x = lrng.randint(40, W - 200)
        w = lrng.randint(90, 260)
        h = lrng.randint(120, 400)
        draw.rectangle([x, floor - h, x + w, floor],
                       fill=(crng.randint(120, 235), crng.randint(120, 230), crng.randint(110, 225)))
    return img


def scene_texture(lrng, crng, palette):
    img = Image.new("RGB", (W, H), palette[crng.randrange(len(palette))])
    draw = ImageDraw.Draw(img, "RGBA")
    for _ in range(lrng.randint(40, 160)):
        _rect(draw, lrng, crng, palette, alpha=(60, 190))
    return img.filter(ImageFilter.GaussianBlur(lrng.uniform(0.0, 2.5)))


def scene_sky(lrng, crng, palette):
    img = Image.new("RGB", (W, H), palette[crng.randrange(len(palette))])
    draw = ImageDraw.Draw(img, "RGBA")
    horizon = lrng.randint(180, 420)
    top = palette[crng.randrange(len(palette))]
    for y in range(horizon):
        t = y / max(1, horizon)
        color = (
            int(top[0] * (1 - t) + 250 * t),
            int(top[1] * (1 - t) + 235 * t),
            int(top[2] * (1 - t) + 210 * t),
        )
        draw.line([(0, y), (W, y)], fill=color)
    for _ in range(lrng.randint(0, 7)):
        cx, cy = lrng.randint(0, W), lrng.randint(0, horizon - 40)
        r = lrng.randint(30, 110)
        draw.ellipse([cx - r, cy - r // 3, cx + r, cy + r // 3], fill=(255, 255, 255, lrng.randint(60, 170)))
    draw.rectangle([0, horizon, W, H],
                   fill=(crng.randint(40, 130), crng.randint(50, 130), crng.randint(30, 100)))
    return img


STYLES = [
    scene_portrait,
    scene_landscape,
    scene_screenshot,
    scene_document,
    scene_cat,
    scene_nature,
    scene_food,
    scene_indoor,
    scene_texture,
    scene_sky,
]


def scene(seed: int, style_index: int, layout_seed: int | None = None) -> Image.Image:
    """Геометрия берётся из layout_seed, содержимое и цвет — из seed.

    Два снимка с одинаковым layout_seed и разными seed получают одну и ту же
    композицию, но разное содержимое: похожий кадр, который дублем не является.
    """
    lrng = random.Random(seed if layout_seed is None else layout_seed)
    crng = random.Random(seed)
    palette = crng.sample(PALETTE, crng.randint(3, len(PALETTE)))
    img = STYLES[style_index % len(STYLES)](lrng, crng, palette)
    img = img.rotate(lrng.uniform(-8, 8), resample=Image.BICUBIC, expand=False, fillcolor=palette[0])
    img = ImageEnhance.Color(img).enhance(crng.uniform(0.55, 1.45))
    img = ImageEnhance.Brightness(img).enhance(crng.uniform(0.65, 1.3))
    img = ImageEnhance.Contrast(img).enhance(crng.uniform(0.7, 1.35))
    img = img.filter(ImageFilter.GaussianBlur(lrng.uniform(0.0, 0.7)))
    draw = ImageDraw.Draw(img)
    draw.text((16, H - 32), f"#{seed}", fill=(255, 255, 255, 190))
    return img


def write(path: Path, img: Image.Image, moment: dt.datetime, *, exif: bool, quality: int = 90) -> None:
    img.save(path, "JPEG", quality=quality)
    stamp = moment.timestamp()
    os.utime(path, (stamp, stamp))
    if not exif:
        return
    try:
        handle = Image.open(path)
        exif_data = handle.getexif()
        exif_data[271] = "Kurator"
        exif_data[272] = "TestCam"
        exif_data[274] = 1
        exif_data[306] = moment.strftime("%Y:%m:%d %H:%M:%S")
        handle.save(path, "JPEG", quality=quality, exif=exif_data)
        handle.close()
        os.utime(path, (stamp, stamp))
    except Exception:
        pass


def build(out: Path, scenes: int) -> dict:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    counts = {"сцен": 0, "точные дубли": 0, "near-дубли": 0, "burst-кадры": 0,
              "ловушки": 0, "мусор": 0}
    rng = random.Random(7)
    base_time = dt.datetime(2024, 3, 10, 9, 0, 0)
    originals: list[tuple[Path, int, int, dt.datetime]] = []
    truth: dict[str, dict] = {}

    for i in range(scenes):
        style = i % len(STYLES)
        seed = 1000 + i * 37
        moment = base_time + dt.timedelta(days=i * 2, minutes=(i * 13) % 700)
        folder = out / f"Photos from {moment.year + (i % 3)}"
        folder.mkdir(exist_ok=True)
        path = folder / f"IMG_{moment.strftime('%Y%m%d_%H%M%S')}.jpg"
        write(path, scene(seed, style), moment, exif=True, quality=rng.randint(80, 95))
        originals.append((path, seed, style, moment))
        truth[path.name] = {
            "rel": str(path.relative_to(out)),
            "scene": seed,
            "style": STYLES[style].__name__,
            "kind": "original",
            "derived_from": None,
        }
        counts["сцен"] += 1

    for source, seed, style, moment in originals:
        roll = rng.random()
        folder = source.parent
        if roll < 0.07:
            clash = folder / f"{source.name[:-4]} (1).jpg"
            if not clash.exists():
                shutil.copy2(source, clash)
                stamp = (moment + dt.timedelta(minutes=2)).timestamp()
                os.utime(clash, (stamp, stamp))
                truth[clash.name] = {
                    "rel": str(clash.relative_to(out)),
                    "scene": seed,
                    "style": STYLES[style].__name__,
                    "kind": "exact-dup",
                    "derived_from": source.name,
                }
                counts["точные дубли"] += 1
        elif roll < 0.14:
            img = Image.open(source)
            scale = rng.uniform(0.82, 0.97)
            resized = img.resize((int(W * scale), int(H * scale)), Image.LANCZOS)
            target = folder / f"{source.stem}_edited.jpg"
            write(target, resized, moment + dt.timedelta(minutes=6), exif=False,
                  quality=rng.choice([38, 52, 66, 80]))
            img.close()
            truth[target.name] = {
                "rel": str(target.relative_to(out)),
                "scene": seed,
                "style": STYLES[style].__name__,
                "kind": "near-dup",
                "derived_from": source.name,
            }
            counts["near-дубли"] += 1
        elif roll < 0.21:
            frames = rng.randint(2, 4)
            for frame in range(frames):
                frame_rng = random.Random(seed * 1000 + frame)
                img = scene(seed, style)
                draw = ImageDraw.Draw(img, "RGBA")
                dx = frame_rng.randint(-7, 7)
                dy = frame_rng.randint(-5, 5)
                draw.rectangle([W // 2 - 40 + dx, 380 + dy, W // 2 + 40 + dx, 384 + dy], fill=(255, 255, 255, 190))
                draw.polygon([(dx, 0), (dx + frame_rng.randint(15, 60), 0)], fill=(255, 255, 255, 70))
                target = folder / f"BURST_{moment.strftime('%Y%m%d_%H%M%S')} ({frame}).jpg"
                write(target, img, moment + dt.timedelta(milliseconds=frame * 350),
                      exif=True, quality=90)
                truth[target.name] = {
                    "rel": str(target.relative_to(out)),
                    "scene": seed,
                    "style": STYLES[style].__name__,
                    "kind": "burst",
                    "derived_from": source.name,
                }
                counts["burst-кадры"] += 1

    for index, (source, seed, style, moment) in enumerate(originals):
        if index % 6:
            continue
        target = source.parent / f"{source.stem}_lookalike.jpg"
        write(target, scene(seed + 900000, style, layout_seed=seed),
              moment + dt.timedelta(minutes=9), exif=True, quality=rng.randint(80, 95))
        truth[target.name] = {
            "rel": str(target.relative_to(out)),
            "scene": seed + 900000,
            "layout_of": seed,
            "style": STYLES[style].__name__,
            "kind": "lookalike",
            "derived_from": source.name,
        }
        counts["ловушки"] += 1

    (out / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    (out / "readme.txt").write_text("не картинка\n", encoding="utf-8")
    (out / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 512)
    (out / "broken.jpg").write_bytes(b"\xff\xd8\xff\xe0this is not a jpeg at all")
    (out / "CAM0099.jpg").write_bytes(b"\xff\xd8\xff\xdb" + b"\x00" * 400)
    hidden = out / ".thumbnails"
    hidden.mkdir(exist_ok=True)
    write(hidden / "cache.jpg", scene(999, 8), base_time, exif=False, quality=70)
    counts["мусор"] += 6

    (out / "ground_truth.json").write_text(
        json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return counts


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/kurator-test")
    scenes = int(sys.argv[2]) if len(sys.argv) > 2 else 180
    counts = build(out, scenes)
    total = sum(1 for p in out.rglob("*") if p.is_file())
    print(f"набор готов: {out}")
    print(f"  файлов всего   : {total}")
    print(f"  ловушек         : {counts['ловушки']} (похожая композиция, другой сюжет)")
    print(f"  ground truth    : {out / 'ground_truth.json'}")
    for key, value in counts.items():
        print(f"  {key:<15}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())