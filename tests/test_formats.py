"""Форматы, которые kurator обещает читать, должны читаться на самом деле.

История этого файла. В списке расширений стояли `.heic` и `.heif`, и в
этом был уверен любой, кто его читал: iPhone, значит must work. На
ARCHиве из 6804 файлов 1620 были `.heic`, и все они молча попадали в
индекс без эмбеддинга и без pHash — в счётчик «нечитаемых». Поломка не
падала и не выглядела поломкой: приложение работало, файлы считались,
просто по ним ничего не находилось.

Две проверки, и обе на то, чтобы список нельзя было разойти с реальностью
молча:

- каждое расширение, кроме сырых, должно быть известно Pillow после
  регистрации. Расширение без декодера — это ровно случай `.heic` до
  подключения pillow-heif;
- каждый формат, который Pillow умеет писать, проверяется настоящим
  круговым тестом: записать файл и прочитать его обратно. Регистратор
  может существовать, а декодер — сломаться.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from kurator import imaging  # noqa: F401 — регистрирует heif при импорте
from kurator.config import HEIF_EXTENSIONS, IMAGE_EXTENSIONS, RAW_EXTENSIONS

# Форматы, которые Pillow умеет писать, и расширение, под которым он их
# пишет. Для них делается настоящий файл и он читается обратно.
WRITABLE = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".jfif": "JPEG",
    ".png": "PNG",
    ".webp": "WEBP",
    ".tif": "TIFF",
    ".tiff": "TIFF",
    ".bmp": "BMP",
    ".dib": "BMP",
    ".gif": "GIF",
    ".jp2": "JPEG2000",
    ".j2k": "JPEG2000",
    ".j2c": "JPEG2000",
    ".avif": "AVIF",
    ".avifs": "AVIF",
    ".heic": "HEIF",
    ".heif": "HEIF",
    ".heics": "HEIF",
    ".pcx": "PCX",
    ".tga": "TGA",
}


def test_every_listed_extension_has_a_decoder():
    """Pillow должен знать декодер для каждого обещанного расширения.

    Сырые исключены осознанно: Pillow открывает их по содержимому, через
    плагин, и в `registered_extensions()` их нет ни при каких условиях —
    там не было бы и `.cr2`, даже будь поддержка в порядке.
    """
    known = {ext.lower() for ext in Image.registered_extensions()}
    without_decoder = sorted(
        (IMAGE_EXTENSIONS - RAW_EXTENSIONS) - known
    )
    assert not without_decoder, (
        "kurator обещает читать эти расширения, но Pillow о них не знает: "
        f"{', '.join(without_decoder)}. Файл будет посчитан, прочитан "
        "не будет и молча уйдёт в «нечитаемые». Либо подключите "
        "декодер, либо уберите расширение из списка."
    )


@pytest.mark.parametrize("ext", sorted(set(IMAGE_EXTENSIONS) & set(WRITABLE)))
def test_format_survives_a_real_round_trip(ext: str) -> None:
    """Настоящий файл этого формата должен открыться обратно."""
    image = Image.new("RGB", (64, 48), (140, 90, 60))
    buffer = io.BytesIO()
    image.save(buffer, format=WRITABLE[ext])
    buffer.seek(0)

    with Image.open(buffer) as got:
        got.load()
        assert got.size == (64, 48)


def test_raw_extensions_are_covered_by_the_general_list() -> None:
    """Сырые не должны выпасть из общего списка.

    RAW_EXTENSIONS выглядит как отдельная сущность, и легко забыть
    вписать его в IMAGE_EXTENSIONS — тогда файлы просто перестанут
    находиться, без всякой ошибки.
    """
    assert RAW_EXTENSIONS <= IMAGE_EXTENSIONS
    assert HEIF_EXTENSIONS <= IMAGE_EXTENSIONS


def test_heic_opener_is_registered_for_real():
    """Регистрация heif должна происходить при импорте kurator.

    `imaging` импортируется ради этого и ни для чего больше. Проверка
    ловит случай, когда регистрация переедет или её уберут, посчитав
    зависимость лишней: снимки с айфона снова станут нечитаемыми, и
    заметить это можно будет только на архиве.
    """
    assert ".heic" in {ext.lower() for ext in Image.registered_extensions()}


def test_heic_capture_date_is_read_from_a_nested_ifd(tmp_path):
    """Дата съёмки HEIC лежит глубже, чем Pillow заходит по умолчанию.

    У файла с айфона в верхнем уровне EXIF есть Make и Model, а
    DateTimeOriginal — в под-IFD, куда Pillow для JPEG заходит сам, а для
    HEIF нет. Без явного спуска снимок получал дату по времени
    копирования файла, и на синхронизируемом архиве она не значила
    ничего. От даты зависят burst-серии и порядок в отчёте.
    """
    from kurator.imaging import _exif_from

    original = Image.new("RGB", (32, 24), (200, 30, 30))
    exif = Image.Exif()
    exif[271] = "Apple"  # верхний уровень
    nested = exif.get_ifd(Image.ExifTags.IFD.Exif)
    nested[36867] = "2024:10:20 21:14:05"

    path = tmp_path / "снимок.heic"
    original.save(path, format="HEIF", exif=exif)

    with Image.open(path) as img:
        got = _exif_from(img)

    assert got["taken_at"] == "2024-10-20T21:14:05", (
        "дата съёмки HEIC не прочитана: "
        f"{got['taken_at']!r}. Без неё снимки с айфона не сортируются и "
        "не собираются в burst-серии."
    )


def test_jpeg_date_still_comes_from_the_usual_place(tmp_path):
    """Правка с под-IFD не должна сломать JPEG.

    Дата берётся из нескольких мест по очереди, и легко испортить тот,
    который работает. Здесь проверяется, что обычный путь не пострадал.
    """
    from kurator.imaging import _exif_from

    image = Image.new("RGB", (32, 24), (30, 200, 30))
    exif = Image.Exif()
    exif[36867] = "2019:07:04 10:00:00"

    path = tmp_path / "обычный.jpg"
    image.save(path, format="JPEG", exif=exif)

    with Image.open(path) as img:
        got = _exif_from(img)

    assert got["taken_at"] == "2019-07-04T10:00:00"
