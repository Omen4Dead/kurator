"""Прогон визуальной башни SigLIP2 через onnxruntime."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

import numpy as np
import onnxruntime as ort
from PIL import Image

from .config import BATCH_SIZE, MODEL_FILENAME, MODEL_SHA256, MODEL_URL
from .imaging import to_siglip_batch

OUTPUT_NAME = "pooler_output"


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
        if not model_is_present(model_path):
            raise FileNotFoundError(
                f"нет весов: {model_path}\n  поставь: ./scripts/pull-model.sh"
            )
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads or int(os.cpu_count() or 4)
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
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
        dummy = np.zeros((1, 3, 224, 224), dtype=np.float32)
        return int(self._run(dummy).shape[1])

    def _run(self, batch: np.ndarray) -> np.ndarray:
        outs = self.session.run([self.output_name], {self.input_name: batch})
        return np.asarray(outs[0], dtype=np.float32)

    def encode(self, images: Iterable[Image.Image]) -> np.ndarray:
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