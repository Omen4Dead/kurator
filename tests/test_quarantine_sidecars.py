"""Служебные файлы macOS не должны ронять удаление из карантина.

На томе exFAT macOS не умеет хранить расширенные атрибуты и кладёт их
рядом отдельным файлом `._имя`. Имя такого файла кончается на `.json`,
если исходный был json, — а `prune_stale_manifests` искал манифесты
ровно по `glob("*.json")` и потому цеплял служебный файл.

Дальше `_load_manifest` читал его как utf-8 и получал
`UnicodeDecodeError` — а это `ValueError`, а не `OSError` и не
`JSONDecodeError`, то есть мимо всех защит. Исключение выходило из
`delete_trash` уже после того, как файлы были стёрты, обработчик
умирал, а пользователь видел «Failed to fetch» и окно, которое не
закрывается.

Стоило этого один символ: `except (OSError, json.JSONDecodeError)`.

Фикстура ниже — побайтовая копия настоящего служебного файла с тома
exFAT: заголовок AppleDouble, а на тридцать седьмом байте 0xb0, из-за
которого и падает разбор utf-8.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from kurator.db import Store
from kurator.quarantine import (
    apply_moves,
    delete_trash,
    list_trash,
    plan_moves,
    prune_stale_manifests,
    quarantine_root,
)

# Настоящий заголовок AppleDouble с тома exFAT: магия, версия, «Mac OS X».
# Первые 36 байт взяты побайтно из реального служебного файла, дальше —
# атрибуты, среди которых байт 0xb0, из-за которого и падает utf-8.
APPLEDOUBLE_HEAD = bytes.fromhex(
    "00051607" "00020000" "4d616320" "4f532058"
    "20202020" "20202020" "20202000" "02000000"
    "09000000" "32000000"
)
APPLEDOUBLE_BODY = b"\x00\x00\x0e\xb0" + bytes(4096 - 40)


def _seed(root: Path) -> Store:
    store = Store.open(root)
    photo = root / "shot.jpg"
    Image.new("RGB", (48, 32), (200, 40, 40)).save(photo, "JPEG")
    store.commit()
    return store


def _write_apple_double(path: Path) -> Path:
    path.write_bytes(APPLEDOUBLE_HEAD + APPLEDOUBLE_BODY)
    return path


def _apple_double_sidecar(target: Path) -> Path:
    """Служебный файл macOS рядом с `target` — как на томе exFAT."""
    return _write_apple_double(target.with_name(f"._{target.name}"))


def test_apple_double_file_is_not_utf8(tmp_path):
    """Служебный файл действительно не utf-8 — иначе проверки ниже пустые."""
    path = _write_apple_double(tmp_path / "._x.json")
    try:
        path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return
    raise AssertionError("служебный файл macOS оказался читаемым как utf-8")


def test_sidecar_of_manifest_does_not_break_delete(tmp_path):
    """Служебный файл рядом с манифестом не должен ронять удаление.

    Воспроизводит настоящую поломку. Нужен манифест с ПУСТЫМ списком
    переносов: такие `delete_trash` пропускает — «ничего не изменилось» —
    и не удаляет, а значит, и его служебный спутник macOS остаётся жив.
    Именно на нём `prune_stale_manifests` и падал.
    """
    store = _seed(tmp_path)
    # Пустой перенос — как он остаётся после переноса, который не удалился.
    apply_moves(store, [], "by-hand", False)
    base = quarantine_root(tmp_path)
    manifest = next(base.glob("manifest_*.json"))
    assert manifest.is_file()
    sidecar = _apple_double_sidecar(manifest)

    apply_moves(store, plan_moves(tmp_path, ["shot.jpg"]), "by-hand", False)
    keys = [i["key"] for i in list_trash(store)]
    assert keys, "в корзине должен быть перенесённый файл"

    # Раньше здесь вылетал UnicodeDecodeError: файлы успевали удалиться,
    # а ответ до клиента не доходил.
    result = delete_trash(store, keys)

    assert result["deleted"] == 1, result
    assert result["failed"] == [], result
    assert list_trash(store) == []
    assert not manifest.is_file(), "пустой манифест остался в карантине"

    # Про самого спутника проверять нельзя: на macOS удаление файла
    # уносит и его `._`-спутника, а на Linux и Windows тот остаётся
    # лишним файлом. Судьба этого файла — дело системы, а не kurator.
    #
    # Важно ровно одно: спутник не должен ничего ломать. Проверяем это
    # вторым кругом — при живом спутнике ещё один перенос и ещё одно
    # удаление. На старом коде именно здесь вылетал UnicodeDecodeError.
    prune_stale_manifests(store)

    second_photo = tmp_path / "shot2.jpg"
    Image.new("RGB", (40, 30), (20, 200, 60)).save(second_photo, "JPEG")
    apply_moves(store, plan_moves(tmp_path, ["shot2.jpg"]), "by-hand", False)
    keys = [i["key"] for i in list_trash(store)]
    assert keys, "второй перенос должен что-то положить в корзину"
    second = delete_trash(store, keys)
    assert second["deleted"] == 1, second
    assert second["failed"] == [], second
    assert list_trash(store) == []

    # И чистка манифестов на этом спутнике тоже не должна падать.
    prune_stale_manifests(store)
    store.close()


def test_empty_manifest_is_cleaned_up(tmp_path):
    """Манифест без переносов не должен болтаться в карантине вечно.

    Пустой манифест описывает ничего: откатывать по нему нечего. Но
    проверка «список переносов не изменился» для пустого списка всегда
    истинна, и такой манифест пропускался — вместе со своим спутником
    macOS, который и ронял удаление.
    """
    store = _seed(tmp_path)
    apply_moves(store, [], "by-hand", False)
    base = quarantine_root(tmp_path)
    manifest = next(base.glob("manifest_*.json"))
    assert manifest.is_file()

    apply_moves(store, plan_moves(tmp_path, ["shot.jpg"]), "by-hand", False)
    delete_trash(store, [i["key"] for i in list_trash(store)])

    assert not manifest.is_file(), "пустой манифест остался в карантине"
    store.close()


def test_sidecar_survives_prune(tmp_path):
    """Чистка манифестов не должна трогать служебные файлы.

    Служебный файл macOS не манифест: он не описывает перенос и не
    должен ни удаляться, ни считаться записью о переносе.
    """
    store = _seed(tmp_path)
    apply_moves(store, plan_moves(tmp_path, ["shot.jpg"]), "by-hand", False)
    base = quarantine_root(tmp_path)
    manifest = next(base.glob("manifest_*.json"))
    sidecar = _apple_double_sidecar(manifest)

    prune_stale_manifests(store)

    assert sidecar.is_file(), "служебный файл macOS удалён чисткой манифестов"
    store.close()


def test_manifest_kept_while_file_alive_pruned_after(tmp_path):
    """Живой манифест сохраняется, а осиротевший — убирается.

    Правка не должна ослабить чистку: пока перенос жив, манифест нужен,
    чтобы откатить. Когда файла не осталось — манифест описывает
    ничего.
    """
    store = _seed(tmp_path)
    apply_moves(store, plan_moves(tmp_path, ["shot.jpg"]), "by-hand", False)
    base = quarantine_root(tmp_path)
    manifest = next(base.glob("manifest_*.json"))

    prune_stale_manifests(store)
    assert manifest.is_file(), "живой манифест убран преждевременно"

    delete_trash(store, [i["key"] for i in list_trash(store)])
    assert not manifest.is_file(), "осиротевший манифест остался в карантине"
    store.close()
