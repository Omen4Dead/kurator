"""Прогон визуальной башни SigLIP2 через onnxruntime."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from .config import BATCH_SIZE, MODEL_FILENAME, MODEL_SHA256, MODEL_URL

if TYPE_CHECKING:  # аннотации — строки, реальных импортов не происходит
    import numpy as np
    from PIL import Image

OUTPUT_NAME = "pooler_output"

# `onnxruntime`, `numpy` и `Pillow` намеренно не импортируются на
# верхнем уровне. Весь этот модуль весит около двухсот мегабайт на диске,
# и раньше они грузились при каждом старте kurator — из-за одного `import`
# в `server.py`, который нужен только для проверки «есть ли веса на диске».
#
# На Windows это стоило минут: каждый открытый файл бандла проходит через
# защитник, и десятки мегабайт библиотек в очереди к нему задерживали
# открытие окна. На macOS было секундой, и правку никто не замечал —
# она выглядит как ускорение, а на деле это разница между «открылось» и
# «не открылось».
#
# Бандл от этого не худеет: PyInstaller находит импорты в любом месте
# функции, а не только на верхнем уровне.


def model_is_present(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 10_000_000


def download_model(path: Path, url: str = MODEL_URL, sha256: str = MODEL_SHA256) -> Path:
    import hashlib
    import urllib.request

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with urllib.request.urlopen(url) as resp, open(tmp, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            block = resp.read(1 << 20)
            if not block:
                break
            out.write(block)
            done += len(block)
            if total:
                print(f"\r  {done / 1e6:7.1f} / {total / 1e6:.1f} МБ", end="", flush=True)
    print()
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
    if digest != sha256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"SHA-256 не совпал для {path.name}\n  ожидал {sha256}\n  получил {digest}"
        )
    tmp.replace(path)
    return path


def fingerprint(path: Path) -> str:
    """Отпечаток файла модели: та же сеть — тот же отпечаток."""
    import hashlib

    size = path.stat().st_size
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(1 << 22)
            if not block:
                break
            digest.update(block)
    return f"{size}:{digest.hexdigest()[:32]}"


class SiglipVision:
    def __init__(self, model_path: Path, threads: int | None = None) -> None:
        import onnxruntime as ort

        if not model_is_present(model_path):
            raise FileNotFoundError(
                f"нет весов: {model_path}\n  поставь: ./scripts/pull-model.sh"
            )
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads or int(os.cpu_count() or 4)
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # Планировщик памяти выключен намеренно. Он заранее размечает
        # адреса всех промежуточных тензоров и резервирует под них буфер
        # целиком — на 400 снимков это 618 МБ против 405 МБ без него, и
        # без него же быстрее на четверть (18.7 с против 25.9 с).
        # Результат вычислений при этом тот же: планировщик меняет
        # раскладку памяти, а не арифметику, и проверено на эталоне.
        options.enable_mem_pattern = False
        self.session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name
        self.output_names = [o.name for o in self.session.get_outputs()]
        self.output_name = OUTPUT_NAME if OUTPUT_NAME in self.output_names else self.output_names[-1]
        self.path = Path(model_path)
        self.fingerprint = fingerprint(self.path)
        self.output_model_id = f"{Path(model_path).name}:{self.fingerprint[:16]}"
        self.dim = self._probe_dim()

    def _probe_dim(self) -> int:
        import numpy as np

        dummy = np.zeros((1, 3, 224, 224), dtype=np.float32)
        return int(self._run(dummy).shape[1])

    def _run(self, batch: np.ndarray) -> np.ndarray:
        import numpy as np

        outs = self.session.run([self.output_name], {self.input_name: batch})
        return np.asarray(outs[0], dtype=np.float32)

    def encode(self, images: Iterable[Image.Image]) -> np.ndarray:
        import numpy as np

        from .imaging import to_siglip_batch

        stack = [to_siglip_batch(img) for img in images]
        if not stack:
            return np.zeros((0, self.dim), dtype=np.float32)
        batch = np.stack(stack).astype(np.float32)
        vec = self._run(batch)
        if vec.ndim == 3:
            vec = vec[:, 0, :]
        norms = np.linalg.norm(vec, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (vec / norms).astype(np.float32)

    def encode_from_paths(self, paths: list[Path], batch_size: int = BATCH_SIZE) -> tuple[list[Path], np.ndarray]:
        import numpy as np

        from .imaging import BadImage, open_rgb

        chunks: list[np.ndarray] = []
        used: list[Path] = []
        for start in range(0, len(paths), batch_size):
            batch_paths = paths[start : start + batch_size]
            images: list[Image.Image] = []
            kept: list[Path] = []
            for path in batch_paths:
                try:
                    images.append(open_rgb(path))
                except BadImage:
                    continue
                kept.append(path)
            if images:
                chunks.append(self.encode(images))
                used.extend(kept)
        if not chunks:
            return [], np.zeros((0, self.dim), dtype=np.float32)
        return used, np.vstack(chunks)


def default_encoder(model_path: Path) -> SiglipVision:
    return SiglipVision(model_path / MODEL_FILENAME if model_path.is_dir() else model_path)