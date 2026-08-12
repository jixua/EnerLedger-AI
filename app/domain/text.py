"""Small text-normalization helpers shared by persistence and API boundaries."""

from __future__ import annotations

import re


_MOJIBAKE_HINT = re.compile(r"(?:Ã|Â|â€|æ|è|å|ç|é|ä|ð|ï¿½|[\x80-\x9f])")
_CJK = re.compile(r"[\u3400-\u9fff]")


def repair_legacy_mojibake(value: str | None) -> str | None:
    """Repair UTF-8 text that was previously decoded as Windows-1252.

    The guard is intentionally conservative: plain Western text is returned as-is,
    and a candidate is accepted only when mojibake markers decrease and readable CJK
    text is recovered.
    """

    if not value or len(_MOJIBAKE_HINT.findall(value)) < 2:
        return value
    try:
        source_bytes = bytes(
            ord(character)
            if ord(character) <= 0xFF
            else character.encode("cp1252")[0]
            for character in value
        )
        candidate = source_bytes.decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError, ValueError):
        return value
    if not _CJK.search(candidate):
        return value
    if len(_MOJIBAKE_HINT.findall(candidate)) >= len(_MOJIBAKE_HINT.findall(value)):
        return value
    return candidate
