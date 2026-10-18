"""Установщик Windows должен собираться — и об этом узнаём мы, а не CI.

Inno Setup различает `{Константа}` и `{{Литерал}}`. Одно-фигурная
скобка означает ссылку на константу, двойная — просто фигурную
скобку. Поэтому `AppId` пишется как `{{GUID}`, и без второй скобки он
читается не как идентификатор приложения, а как имя константы.

Стоило такой опечатки одного символа: сборка macOS проходила, CI
проверял бандл и падал позже, на этапе Inno Setup, строкой в двадцать
восьмой — далеко от места, где испорчено. Проверка бандла про это
знать не могла, она про другое.

Здесь ловится класс целиком: любая `{...}` в секции `[Setup]`
должна быть либо определением из `#define`, либо одной из встроенных
констант Inno Setup, либо `{{...}` литералом.
"""

from __future__ import annotations

import re
from pathlib import Path

ISS = Path(__file__).resolve().parent.parent / "packaging" / "installer.iss"

# Константы, которые Inno Setup знает сам. Всё остальное в `{...}`
# должно быть либо `#define` из этого же файла, либо `{{`-литералом.
INNO_BUILTIN = {
    "app", "autopf", "userpf", "pf", "pf32", "pf64",
    "commonpf", "commonpf32", "commonpf64",
    "win", "sys", "syswow64", "windows", "tmp", "fonts", "dao", "src",
    "group", "userdocs", "userappdata", "commonappdata", "localappdata",
    "userdesktop", "commondesktop", "userstartmenu", "commonstartmenu",
    "userprograms", "commonprograms", "userstartup", "commonstartup",
    "userprofile", "commonfiles", "exe", "code", "reg", "sdk", "olddata",
}


def _defines(text: str) -> set[str]:
    return set(re.findall(r"^#define\s+(\w+)", text, re.M))


def _setup_section(text: str) -> str:
    start = text.index("[Setup]")
    rest = text[start + len("[Setup]"):]
    end = rest.find("\n[")
    return rest[:end] if end != -1 else rest


def test_setup_section_has_no_unknown_constants():
    """Каждая `{...}` в [Setup] должна быть известна Inno Setup."""
    text = ISS.read_text(encoding="utf-8")
    defines = _defines(text)
    section = _setup_section(text)

    unknown: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(";") or stripped.startswith("#"):
            continue
        # `{{...}` — литеральная фигурная скобка, такую Inno выводит как есть.
        scrubbed = re.sub(r"\{\{[^\n]*?\}", "", line)
        for token in re.findall(r"\{([^{}]*)\}", scrubbed):
            name = token.lstrip("#").strip()
            if name in defines or name in INNO_BUILTIN:
                continue
            unknown.append(f"{stripped.split('=')[0].strip()} → {{{token}}}")

    assert not unknown, (
        "в секции [Setup] есть ссылки на неизвестные константы: "
        + "; ".join(unknown)
        + ". Если это идентификатор приложения, он пишется с двумя скобками: "
          "AppId={{GUID}, а не AppId={GUID}."
    )


def test_app_id_is_a_literal_guid():
    """AppId должен быть GUID в двойных скобках и оставаться тем же самым."""
    text = ISS.read_text(encoding="utf-8")
    match = re.search(r"^AppId=(\{+)([0-9A-Fa-f-]+)(\}?)$", text, re.M)
    assert match, "AppId не найден или это не GUID"

    open_braces, guid, close = match.groups()
    assert open_braces == "{{", (
        f"AppId начинается с {open_braces!r}, а должен — с '{{{{': "
        "одиночная скобка означает ссылку на константу, и Inno Setup "
        "отвечает «Unknown constant»"
    )
    assert close == "}", "у AppId должна закрываться ровно одна скобка"
    assert len(guid) == 36 and guid.count("-") == 4, f"AppId не похож на GUID: {guid}"