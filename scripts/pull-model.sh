#!/usr/bin/env bash
# Скачать веса визуальной башни SigLIP2 (90 МБ, Apache-2.0).
# Сеть нужна только здесь. Дальше kurator работает офлайн.
set -euo pipefail

MODEL_REPO="${MODEL_REPO:-onnx-community/siglip2-base-patch16-224-ONNX}"
MODEL_FILE="onnx/vision_model_int8.onnx"
MODEL_SHA256="0dd31785a2713f1113ef2272472165c69d580473dae38d7b47568ac587795e70"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="${1:-$ROOT/models}"
DEST="$DEST_DIR/vision_model_int8.onnx"

# Хеш сверяется всегда, и у «уже на месте» тоже: без этого несовпадение
# молча проходило бы как успех.
sha256_of() {
  shasum -a 256 "$1" | awk '{print $1}'
}

mkdir -p "$DEST_DIR"

if [[ -f "$DEST" ]]; then
  have="$(sha256_of "$DEST")"
  if [[ "$have" == "$MODEL_SHA256" ]]; then
    echo "уже на месте: $DEST"
    exit 0
  fi
  echo "хеш не сходится, перекачиваю: $DEST"
  rm -f "$DEST"
fi

echo "качаю $MODEL_REPO/$MODEL_FILE -> $DEST"
curl -fL --progress-bar \
  "https://huggingface.co/$MODEL_REPO/resolve/main/$MODEL_FILE" \
  -o "$DEST"

echo "проверяю sha256"
have="$(sha256_of "$DEST")"
if [[ "$have" != "$MODEL_SHA256" ]]; then
  echo "SHA-256 не совпал" >&2
  echo "  ожидал: $MODEL_SHA256" >&2
  echo "  получил: $have" >&2
  exit 1
fi

echo "ок, $(du -h "$DEST" | awk '{print $1}')"