"""Сырые файлы камер.

Форматы RAW на CI не положить: диск с фотографиями у разработчика есть, у
раннера нет. Поэтому проверяется механизм, а не конкретный файл — RAW
читается Pillow как TIFF, потому что это TIFF-контейнер, и всё решение
стоит на двух вещах: расширение в списке и умение уменьшить кадр там,
где `draft` не срабатывает.

Настоящий CR2 с машины разработчика проверялся отдельно и ведёт себя
ровно так: открывается как TIFF на 5184×3456, превью внутри настоящее,
хеши считаются.
"""

from __future__ import annotations

from PIL import Image

from kurator.config import IMAGE_EXTENSIONS, INPUT_SIZE, RAW_EXTENSIONS
from kurator.imaging import load_for_analysis
from kurator.indexer import walk_media


def _raw_like(path, size=(400, 300)):
    """TIFF-контейнер, сохранённый под RAW-расширением.

    Настоящий CR2 устроен сложнее, но Pillow открывает его именно так —
    по сигнатуре TIFF, а не по расширению. Значит и проверять надо
    контейнер: так тест работает на любой машине.
    """
    Image.new("RGB", size, (120, 90, 160)).save(path, format="TIFF")
    return path


def test_raw_extensions_are_indexed():
    """RAW обязан попадать в обход папки.

    Иначе файлы не просто не читаются, а молча выпадают: отчёт говорит
    «всего 1713», пока на диске 2549, и цифры выглядят правдоподобно.
    """
    for suffix in (".cr2", ".nef", ".arw", ".orf", ".rw2", ".dng"):
        assert suffix in RAW_EXTENSIONS, f"{suffix} не в списке RAW"
        assert suffix in IMAGE_EXTENSIONS, f"{suffix} не попадёт в обход"


def test_unsupported_raw_is_not_claimed():
    """Форматы, которые Pillow не откроет, добавлять нельзя.

    RAF (Fujifilm) и X3F (Sigma) устроены не как TIFF. Если их внести,
    каждый такой файл упадёт в «нечитаемые», и поддержка RAW окажется
    поддержкой с ошибкой в каждом втором архиве.
    """
    for suffix in (".raf", ".x3f"):
        assert suffix not in RAW_EXTENSIONS, (
            f"{suffix} не читается Pillow — добавлять значит обещать "
            "то, что не сработает"
        )


def test_raw_file_is_seen_by_walk_whatever_the_case(tmp_path):
    """`.CR2` с верхним регистром должен попадать так же, как `.cr2`.

    На томе с настоящими фотографиями лежат именно `IMG_3678.CR2`, и
    сравнение регистрозависимое увидело бы ноль файлов.
    """
    _raw_like(tmp_path / "IMG_3678.CR2")

    found = {rel for rel, _ in walk_media(tmp_path)}

    assert found == {"IMG_3678.CR2"}, f"обход не увидел RAW: {found}"


def test_load_for_analysis_reads_raw_container(tmp_path):
    """Кадр из TIFF-контейнера читается и приводится к RGB."""
    path = _raw_like(tmp_path / "shot.dng", size=(600, 400))

    rgb, width, height, _ = load_for_analysis(path)

    assert rgb.mode == "RGB"
    assert (width, height) == (600, 400), "исходные размеры потеряны"
    assert max(rgb.size) <= INPUT_SIZE * 2, f"кадр не уменьшен: {rgb.size}"


def test_frame_is_small_even_where_draft_cannot_help(tmp_path):
    """Где `draft` не сработал, кадр всё равно уменьшается.

    Раньше на CR2 кадр оставался 5184×3456, и `convert` делал его вторую
    копию — 240 МБ пика на один файл. Проверяется на TIFF: `draft` на нём
    не работает, ровно как на сыром файле камеры.
    """
    path = _raw_like(tmp_path / "big.dng", size=(4000, 3000))

    rgb, _, _, _ = load_for_analysis(path)

    assert max(rgb.size) <= INPUT_SIZE * 2, f"кадр остался {rgb.size}"


def test_jpeg_path_is_left_exactly_as_it_was(tmp_path):
    """Для JPEG путь не меняется: `draft` срабатывает, уменьшать нечего.

    Это не только про экономию. Лишняя передискретизация двигает
    эмбеддинги: замер на настоящих снимках дал расхождение косинуса
    0.028. У JPEG этого быть не должно вовсе, иначе правка RAW портила бы
    то, что и так работало.
    """
    path = tmp_path / "photo.jpg"
    Image.new("RGB", (3000, 2000), (30, 60, 90)).save(path, format="JPEG", quality=92)

    with Image.open(path) as img:
        before = max(img.size)
        img.draft("RGB", (INPUT_SIZE, INPUT_SIZE))
        after_draft = max(img.size)

    assert after_draft < before, "draft не сработал на JPEG — тест бессмысленен"

    rgb, _, _, _ = load_for_analysis(path)

    assert max(rgb.size) == after_draft, (
        f"кадр уменьшен лишний раз: {rgb.size} вместо {after_draft}"
    )