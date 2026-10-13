"""Хеши: md5 байтов, перцептивный DCT-хеш и градиентный хеш."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

DCT_SIZE = 32
DCT_KEEP = 8
DHASH_W = 9
DHASH_H = 8

_U64 = np.uint64


def md5_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _dct_matrix(n: int) -> np.ndarray:
    k = np.arange(n).reshape(-1, 1).astype(np.float64)
    x = np.arange(n).reshape(1, -1).astype(np.float64)
    m = np.cos(np.pi * (2 * x + 1) * k / (2 * n))
    m[0] *= 1.0 / np.sqrt(2.0)
    return m * np.sqrt(2.0 / n)


_DCT = _dct_matrix(DCT_SIZE)


def _bits_to_int(bits: np.ndarray) -> int:
    packed = np.packbits(bits.astype(np.uint8).ravel())
    return int.from_bytes(packed.tobytes(), "big")


def phash(gray32: np.ndarray) -> int:
    """DCT-хеш: переживает смену качества JPEG, ресайз и мелкие правки."""
    if gray32.shape != (DCT_SIZE, DCT_SIZE):
        raise ValueError(f"phash ждёт {DCT_SIZE}x{DCT_SIZE}, получил {gray32.shape}")
    coef = _DCT @ gray32.astype(np.float64) @ _DCT.T
    block = coef[:DCT_KEEP, :DCT_KEEP].ravel()[1:]
    return _bits_to_int(block > np.median(block))


def dhash(gray9x8: np.ndarray) -> int:
    """Градиентный хеш: инвариантен к общей яркости, ловит сдвиги кадра."""
    if gray9x8.shape != (DHASH_H, DHASH_W):
        raise ValueError(f"dhash ждёт {DHASH_H}x{DHASH_W}, получил {gray9x8.shape}")
    return _bits_to_int(gray9x8[:, 1:] > gray9x8[:, :-1])


def to_hex(value: int) -> str:
    return f"{value:016x}"


def from_hex(text: str) -> int:
    return int(text, 16)


def hamming(a: int, b: int) -> int:
    return int(np.bitwise_count(_U64(a) ^ _U64(b)))


def hamming_many(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.bitwise_count(np.bitwise_xor(a, b)).astype(np.int16)