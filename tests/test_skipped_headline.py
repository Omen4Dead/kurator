"""Шапка списка находок не должна враньить про пропущенные группы.

Пропуск группы прячет её за полосу «оставлено как есть». Нажать на
такую группу уже нельзя — на месте неё кнопка «Вернуть в разбор».
Но счётчик в шапке продолжал называть её полноценной группой, а
сумма отмеченного продолжала включать её отметки, потому что при
пропуске `decide()` выходит раньше, не чистя `state.selected`.

Человек видел «4 групп · отмечено на 111.7 КБ» и понимал это как
«есть что переносить», хотя переносить было нечего.

Проверка статическая, а не по поведению: node в CI нет, а логика
живёт в браузере. Поэтому тест ловит форму исходника, и это его
честное ограничение — он защищает от возврата именно этого счёта,
но не ловит ошибку в формулировке текста.
"""

from __future__ import annotations

import re
from pathlib import Path

APP_JS = (
    Path(__file__).resolve().parent.parent / "src" / "kurator" / "web" / "assets" / "app.js"
)


def _render_body() -> str:
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("function render()")
    end = source.index("function findGroupByKey")
    return source[start:end]


def test_headline_never_uses_the_raw_group_count():
    """Шапка не должна считать пропущенные группы как рабочие."""
    body = _render_body()
    assert "groups.length} групп" not in body, (
        "шапка снова считает все группы подряд, включая пропущенные: "
        "на них уже нельзя нажать, и число обещает работу, которой нет"
    )


def test_headline_is_built_from_a_skip_filtered_list():
    """Число групп в шапке берётся из списка без пропущенных."""
    body = _render_body()
    filtered = re.search(r"const (\w+) = groups\.filter\(\(g\) => !", body)
    assert filtered, "в render() нет списка групп без пропущенных"
    name = filtered.group(1)

    start = body.index("const headline")
    # `main.innerHTML` встречается в render() дважды: в раннем возврате для
    # пустого списка и в конце. Берём последнее вхождение, иначе блок
    # заголовка оказывается пустым и проверка ничего не проверяет.
    block = body[start:body.rindex("main.innerHTML")]
    assert f"{name}.length" in block, (
        f"заголовок собирается не из `{name}`, а из общего числа групп"
    )


def test_selected_bytes_exclude_skipped_groups():
    """«Отмечено на» считает только по группам в работе."""
    body = _render_body()
    filtered = re.search(r"const (\w+) = groups\.filter\(\(g\) => !", body)
    assert filtered, "в render() нет списка групп без пропущенных"
    name = filtered.group(1)
    total = re.search(r"const total = (\w+)\.reduce", body)
    assert total, "не найден подсчёт отмеченного"
    assert total.group(1) == name, (
        f"размер отмеченного считается по `{total.group(1)}`, а не по `{name}`: "
        "отметки пропущенной группы остаются в памяти и попадают в сумму"
    )


def test_explanation_disappears_when_every_group_is_skipped():
    """Пояснение про рамки не должно висеть, когда выбирать нечего."""
    body = _render_body()
    assert "const howto = live.length" in body, (
        "пояснение про красную рамку показывается даже тогда, когда все "
        "группы оставлены как есть и рамок на экране нет"
    )


def test_plural_helper_exists_and_covers_russian_rules():
    """Склонение для новых надписей должно быть, а не выдумано на лету."""
    source = APP_JS.read_text(encoding="utf-8")
    assert "function plural(" in source, "в app.js нет plural, а надписи его зовут"
    body = source[source.index("function plural("):source.index("function fmtBytes")]
    assert "mod100 >= 11" in body and "mod100 <= 14" in body, (
        "plural не разбирает исключение 11–14: получится «11 оставлено», "
        "а правильно «11 оставлено» — как есть, но 21 обязан быть «оставлена»"
    )