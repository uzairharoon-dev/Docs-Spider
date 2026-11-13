"""Docs version detection from URLs (F4).

``/0.115/tutorial/`` -> ``0.115``, ``/v2/guide/`` -> ``v2``, ``/en/stable/`` -> ``stable``,
``/3.12/library/os.html`` -> ``3.12``. Only the first few directory segments are looked at,
so ``/api/v1/users`` reference pages deep in a site don't get mistaken for a docs version.
A custom regex (``version_pattern``) overrides the heuristic.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

_SEGMENT = re.compile(
    r"^v?\d+(?:\.\d+){0,2}(?:\.x)?(?:[-.]?(?:a|b|alpha|beta|rc|dev|post)\d*)?$", re.IGNORECASE
)
_PRERELEASE = re.compile(r"(?:a|b|alpha|beta|rc|dev)\d*$", re.IGNORECASE)
ALIASES = ("latest", "stable")
MAX_SEGMENTS = 3


def compile_pattern(pattern: str) -> re.Pattern[str]:
    rx = re.compile(pattern)
    if rx.groups == 0:
        raise ValueError("version_pattern needs a capture group, e.g. ^/docs/(?P<version>[^/]+)/")
    return rx


def detect_version(url: str, pattern: str | re.Pattern[str] | None = None) -> str | None:
    path = urlsplit(url).path or "/"
    if pattern is not None:
        rx = compile_pattern(pattern) if isinstance(pattern, str) else pattern
        m = rx.search(path)
        if m is None:
            return None
        return m.group("version") if "version" in rx.groupindex else m.group(1)
    segments = [s for s in path.split("/") if s]
    directories = segments if path.endswith("/") else segments[:-1]  # last one is the page
    for seg in directories[:MAX_SEGMENTS]:
        if _SEGMENT.match(seg) or seg.lower() in ALIASES:
            return seg.lower() if seg.lower() in ALIASES else seg
    return None


def version_key(version: str) -> tuple:
    """Sort key: numeric versions by their numbers, pre-releases below the release."""
    m = re.match(r"^v?(\d+(?:\.\d+)*)", version, re.IGNORECASE)
    if m is None:
        return (0, (), 0)
    nums = tuple(int(n) for n in m.group(1).split("."))
    pre = _PRERELEASE.search(version[m.end():])
    if pre is None:
        return (1, nums, 1, 0)
    pre_num = re.search(r"\d+$", pre.group(0))
    return (1, nums, 0, int(pre_num.group(0)) if pre_num else 0)


def pick_latest(versions: Iterable[str]) -> str | None:
    """The version 'latest' should mean.

    The highest numbered release (a pre-release only when no release exists). Without
    numbered versions: the literal 'latest' label, then 'stable'. The default label of
    unversioned pages ('latest') therefore never hides /v2/-style versions.
    """
    vs = list(versions)
    if not vs:
        return None
    numbered = [v for v in vs if version_key(v)[0] == 1]
    releases = [v for v in numbered if version_key(v)[2] == 1]
    if releases or numbered:
        return max(releases or numbered, key=version_key)
    for alias in ("latest", "stable"):
        if alias in vs:
            return alias
    return sorted(vs)[-1]
