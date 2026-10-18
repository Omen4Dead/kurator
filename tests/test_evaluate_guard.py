"""`evaluate` не должен выглядеть успешным, ничего не проверив.

Найдено на живом прогоне: `evaluate` не индексирует сам, и при забытом
`kurator index` печатал шесть строк метрик по 1.000 — при том, что не
проверено ни одного файла. Выглядит это как идеальный результат, и
именно поэтому опасно: цифры красивые, а под ними ноль работы.
"""

from __future__ import annotations

import json
from pathlib import Path

from kurator.cli import main


def _write_truth(root: Path) -> None:
    """Разметка есть, файлов нет — ровно то состояние, которое ловим."""
    (root / "ground_truth.json").write_text(
        json.dumps(
            {
                "снимок.jpg": {
                    "rel": "снимок.jpg",
                    "kind": "original",
                    "sha256": "0" * 64,
                    "cluster": 1,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_evaluate_fails_loudly_on_an_empty_index(tmp_path, capsys):
    """Пустой индекс — это провал, а не результат 1.000 по всем метрикам."""
    _write_truth(tmp_path)

    code = main(["evaluate", str(tmp_path)])
    out = capsys.readouterr().out

    assert code == 1, "пустой индекс должен давать ненулевой код возврата"
    assert "ПРОВАЛ" in out, f"в выводе нет слова о провале:\n{out}"
    assert "kurator index" in out, (
        "сообщение должно подсказывать, что делать дальше, а не просто "
        f"констатировать провал:\n{out}"
    )
    # Главное: таблицы с единицами быть не должно вовсе.
    assert "точные дубликаты" not in out, (
        f"метрики напечатаны, хотя проверять было нечего:\n{out}"
    )


def test_evaluate_passes_when_there_is_something_to_check(tmp_path, capsys):
    """Защита не должна рубить нормальный прогон."""
    image = tmp_path / "снимок.jpg"
    image.write_bytes(Path("assets/icon-256.png").read_bytes())
    _write_truth(tmp_path)

    from kurator.indexer import index_root
    from kurator.db import Store

    store = Store.open(tmp_path)
    index_root(tmp_path, store)
    store.close()

    code = main(["evaluate", str(tmp_path)])
    out = capsys.readouterr().out

    assert code == 0, f"нормальный прогон не должен падать:\n{out}"
    assert "точные дубликаты" in out, f"метрики должны печататься:\n{out}"
