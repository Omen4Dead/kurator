#!/usr/bin/env bash
# Сборка kurator.app и установщика kurator.dmg для macOS (Apple Silicon).
#
# Приложение состоит из двух частей:
#   Contents/MacOS/kurator-app     — нативный SwiftUI-интерфейс (app/);
#   Contents/Resources/backend/    — python-бэкенд, собранный PyInstaller.
#
# Что делает скрипт:
#   1. собирает бэкенд через PyInstaller (packaging/kurator.spec);
#   2. собирает интерфейс через swift build;
#   3. складывает из них kurator.app;
#   4. подписывает ad-hoc — без подписи macOS не даст открыть файл;
#   5. собирает .dmg с перетаскиванием в «Программы».
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
BACKEND="$DIST/backend"

if [ "$(uname -m)" != "arm64" ]; then
  echo "предупреждение: собрано под $(uname -m), приложение рассчитано на Apple Silicon" >&2
fi

echo "==> веса модели"
# Одна команда должна работать из свежего клона. Сеть по-прежнему только
# здесь — внутри pull-model.sh, и больше нигде в сборке.
if [ ! -f "models/vision_model_int8.onnx" ]; then
    ./scripts/pull-model.sh
else
    echo "  уже на месте"
fi

echo "==> бэкенд (PyInstaller, onedir)"
rm -rf "$APP" "$BACKEND" "$DIST/build"
uv run --group dev pyinstaller \
  --noconfirm \
  --clean \
  --distpath "$DIST" \
  --workpath "$DIST/build" \
  packaging/kurator.spec

echo "==> интерфейс (swift build)"
# Интерфейс собирается релизно: отладочный бинарь заметно медленнее
# и при этом ничего не даёт тому, кто просто ставит приложение.
(cd app && swift build -c release)
SWIFT_BIN="app/.build/release/kurator-app"
[ -x "$SWIFT_BIN" ] || { echo "нет собранного $SWIFT_BIN" >&2; exit 1; }

echo "==> сборка $APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$SWIFT_BIN" "$APP/Contents/MacOS/kurator-app"
cp -R "$BACKEND" "$APP/Contents/Resources/backend"
cp "assets/kurator.icns" "$APP/Contents/Resources/kurator.icns"
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>kurator-app</string>
  <key>CFBundleIdentifier</key>
  <string>ru.omen4dead.kurator</string>
  <key>CFBundleName</key>
  <string>kurator</string>
  <key>CFBundleDisplayName</key>
  <string>kurator</string>
  <key>CFBundleShortVersionString</key>
  <string>0.7.0</string>
  <key>CFBundleVersion</key>
  <string>0.7.0</string>
  <key>CFBundleIconFile</key>
  <string>kurator</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>LSMinimumSystemVersion</key>
  <string>14.0</string>
  <key>NSAppTransportSecurity</key>
  <dict>
    <!-- Приложение только слушает локальный порт и ходит в интернет
         не умеет вовсе: разрешать ему сеть незачем. -->
    <key>NSAllowsArbitraryLoads</key>
    <false/>
    <!-- А вот к собственному бэкенду на 127.0.0.1 обращаться надо,
         иначе интерфейс не увидит ни одного ответа. -->
    <key>NSAllowsLocalNetworking</key>
    <true/>
  </dict>
</dict>
</plist>
EOF

echo "==> подпись ad-hoc"
# Подписывать надо изнутри наружу: сначала содержимое, потом бандл.
find "$APP/Contents" -type f \( -name "*.dylib" -o -name "*.so" \) -exec \
  codesign --force --sign - {} \; 2>/dev/null || true
codesign --force --sign - --deep "$APP"

echo "==> проверка подписи"
codesign --verify --deep --strict "$APP" && echo "подпись в порядке"

# Проверяем собранное приложение, а не только факт сборки. Сборка, которая
# создалась, но не отвечает по API или не считает эмбеддинги, считалась
# бы успехом: так ловился неполный бандл onnxruntime, и выглядело это как
# «приложение собралось».
echo "==> проверка собранного приложения"
uv run python packaging/verify_bundle.py "$APP"

# Сборка приложения уже закончена, а установщик собирать необязательно.
# Так CI проверяет бандл на каждом пуше, где тронута сборка, не тратя
# ещё и время на упаковку образа.
if [ "${KURATOR_NO_INSTALLER:-0}" = "1" ]; then
    echo
    echo "==> установщик пропущен (KURATOR_NO_INSTALLER=1)"
    du -sh "$APP" | sed 's/^/  приложение  /'
    echo "  путь: $APP"
    exit 0
fi

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
