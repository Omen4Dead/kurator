import math

import pytest
from PIL import Image

from kurator.hashing import DCT_SIZE, DHASH_H, DHASH_W, dhash, from_hex, hamming, md5_file, phash, to_hex


def make_scene(seed: int, size=(640, 480)) -> Image.Image:
    img = Image.new("RGB", size, (40 + seed * 7 % 200, 60, 90))
    draw = ImageDraw.Draw(img)
    for i in range(12):
        x = (seed * 37 + i * 53) % (size[0] - 60)
        y = (seed * 91 + i * 29) % (size[1] - 60)
        draw.rectangle([x, y, x + 40 + i * 3, y + 30 + i * 2], fill=(200 - i * 10, 90 + i * 12, 60))
    return img


import io

from PIL import ImageDraw


def views(img: Image.Image) -> tuple[int, int]:
    gray = img.convert("L")
    dct = gray.resize((DCT_SIZE, DCT_SIZE), Image.LANCZOS)
    grad = gray.resize((DHASH_W, DHASH_H), Image.LANCZOS)
    return phash(np.asarray(dct)), dhash(np.asarray(grad))


import numpy as np


def test_phash_hex_roundtrip():
    for value in (0, 1, 2**63, 2**64 - 1, 12345678901234567890):
        assert from_hex(to_hex(value)) == value


def test_phash_rejects_wrong_shape():
    with pytest.raises(ValueError):
        phash(np.zeros((16, 16), dtype=np.uint8))


def test_dhash_rejects_wrong_shape():
    with pytest.raises(ValueError):
        dhash(np.zeros((4, 4), dtype=np.uint8))


def test_same_scene_same_hash():
    a, _ = views(make_scene(3))
    b, _ = views(make_scene(3))
    assert a == b
    assert hamming(a, b) == 0


def test_phash_survives_jpeg_quality_and_scale():
    original = make_scene(11)
    for quality in (35, 60, 95):
        buf = io.BytesIO()
        original.save(buf, "JPEG", quality=quality)
        buf.seek(0)
        reencoded = Image.open(buf).convert("RGB")
        a, _ = views(original)
        b, _ = views(reencoded)
        assert hamming(a, b) <= 6, f"quality={quality} distance={hamming(a, b)}"

    scaled = original.resize((320, 240), Image.LANCZOS)
    a, _ = views(original)
    b, _ = views(scaled)
    assert hamming(a, b) <= 10


def test_phash_separates_different_scenes():
    base = views(make_scene(1))[0]
    distances = [hamming(base, views(make_scene(seed))[0]) for seed in range(2, 30)]
    assert max(distances) > 6, "разные сцены должны различаться"


def test_dhash_ignores_global_brightness():
    original = make_scene(5)
    bright = Image.new("RGB", original.size, (255, 255, 255))
    bright.paste(original, (0, 0))
    dark = Image.new("RGB", original.size, (0, 0, 0))
    dark.paste(original, (0, 0))
    a = views(original)[1]
    assert hamming(views(bright)[1], a) <= 4
    assert hamming(views(dark)[1], a) <= 4


def test_hamming_is_symmetric():
    a, _ = views(make_scene(2))
    b, _ = views(make_scene(9))
    assert hamming(a, b) == hamming(b, a)


def test_md5_file(tmp_path):
    target = tmp_path / "x.bin"
    payload = b"kurator" * 5000
    target.write_bytes(payload)
    import hashlib

    assert md5_file(target) == hashlib.md5(payload).hexdigest()