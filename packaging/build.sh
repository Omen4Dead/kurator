#!/usr/bin/env bash
# Сборка kurator.app и установщика kurator.dmg для macOS.
#
# Что делает:
#   1. собирает .app через PyInstaller;
#   2. подписывает ad-hoc — без подписи macOS не даст открыть файл;
#   3. собирает .dmg с перетаскиванием в «Программы».
#
# Подпись ad-hoc бесплатна, но у неё есть цена: при первом запуске
# macOS спросит подтверждение, и снять его можно через правый клик →
# «Открыть». Полностью бесшовная установка требует Developer ID
# и нотаризации, это 99 долларов в год.
set -euo pipefail

cd "$(dirname "$0")/.."

DIST="dist"
NAME="kurator"
APP="$DIST/$NAME.app"
DMG="$DIST/$NAME.dmg"

if [ "$(uname -m)" != "arm64" ]; then
  echo "предупреждение: собрано под $(uname -m), приложение рассчитано на Apple Silicon" >&2
fi

echo "==> сборка $APP"
rm -rf "$APP" "$DIST/build"
uv run --group dev pyinstaller \
  --noconfirm \
  --clean \
  --distpath "$DIST" \
  --workpath "$DIST/build" \
  packaging/kurator.spec

echo "==> подпись ad-hoc"
# Подписывать надо изнутри наружу: сначала содержимое, потом бандл.
find "$APP/Contents" -type f \( -name "*.dylib" -o -name "*.so" \) -exec \
  codesign --force --sign - {} \; 2>/dev/null || true
codesign --force --sign - --deep "$APP"

echo "==> проверка подписи"
codesign --verify --deep --strict "$APP" && echo "подпись в порядке"

echo "==> установщик $DMG"
rm -f "$DMG"
STAGE="$DIST/dmg"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Программы"
cat > "$STAGE/Прочитать меня.txt" <<'EOF'
kurator

Перетащите kurator.app в «Программы» и запустите.

При первом запуске macOS спросит, откуда взялось приложение: это
проверка подписи. Нажмите «Открыть» ещё раз — и всё, спрашивать
больше не будет. Это нормально для программ без Developer ID.

Приложение работает только на Apple Silicon и только офлайн.
EOF
# ULFO — это LZFSE, он жмёт заметно сильнее zlib из UDZO, а разница
# в размере образа здесь и решает, влезет ли установщик в лимит GitHub.
hdiutil create -volname "kurator" -srcfolder "$STAGE" -ov -format ULFO "$DMG" >/dev/null
rm -rf "$STAGE"

echo
echo "готово:"
du -sh "$APP" | sed 's/^/  приложение  /'
du -sh "$DMG" | sed 's/^/  установщик  /'
echo "  путь: $DMG"