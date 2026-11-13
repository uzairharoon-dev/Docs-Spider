"""Exact and near-duplicate page detection (F9) using SHA-256 and 64-bit SimHash."""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict

_WORD = re.compile(r"\w+")
_BANDS = 4
_BAND_BITS = 64 // _BANDS
_BAND_MASK = (1 << _BAND_BITS) - 1


def content_hash(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def simhash(text: str, shingle: int = 3) -> int:
    tokens = _WORD.findall(text.lower())
    if not tokens:
        return 0
    if len(tokens) < shingle:
        shingles = Counter([" ".join(tokens)])
    else:
        shingles = Counter(
            " ".join(tokens[i : i + shingle]) for i in range(len(tokens) - shingle + 1)
        )
    weights = [0] * 64
    for sh, count in shingles.items():
        h = int.from_bytes(hashlib.blake2b(sh.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += count if (h >> bit) & 1 else -count
    out = 0
    for bit, w in enumerate(weights):
        if w > 0:
            out |= 1 << bit
    return out


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


class DedupIndex:
    """Finds a stored page whose SimHash is within ``max_distance`` bits.

    Splits each hash into 4 bands of 16 bits. Two hashes at distance <= 3 share
    at least one band exactly, so only same-band candidates need comparing.
    """

    def __init__(self, max_distance: int = 3):
        if max_distance >= _BANDS:
            raise ValueError(f"max_distance must be < {_BANDS} for the band index to be exact")
        self.max_distance = max_distance
        self._bands: dict[tuple[int, int], list[tuple[int, str]]] = defaultdict(list)
        self._exact: dict[str, str] = {}

    @staticmethod
    def _keys(h: int) -> list[tuple[int, int]]:
        return [(i, (h >> (i * _BAND_BITS)) & _BAND_MASK) for i in range(_BANDS)]

    def add(self, url: str, chash: str, h: int) -> None:
        self._exact.setdefault(chash, url)
        for key in self._keys(h):
            self._bands[key].append((h, url))

    def find(self, chash: str, h: int, exclude_url: str | None = None) -> str | None:
        exact = self._exact.get(chash)
        if exact is not None and exact != exclude_url:
            return exact
        for key in self._keys(h):
            for other, url in self._bands.get(key, ()):
                if url != exclude_url and hamming(h, other) <= self.max_distance:
                    return url
        return None
