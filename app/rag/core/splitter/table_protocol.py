"""Canonicalize marker-wrapped tables at the splitter contract boundary."""

from __future__ import annotations

import re

_START_RE = re.compile(
    r"^\s*<!--\s*LINKPARSE_TABLE_START\s+(?P<attrs>.*?)\s*-->\s*$",
    re.IGNORECASE,
)
_END_RE = re.compile(
    r'^\s*<!--\s*LINKPARSE_TABLE_END\s+id="(?P<id>[^"]+)"\s*-->\s*$',
    re.IGNORECASE,
)
_ATTR_RE = re.compile(r'([A-Za-z_][\w-]*)="([^"]*)"')


def extract_linkparse_table_body(content: str) -> str | None:
    """Return the exact table body when a valid, paired marker envelope is present."""

    lines = content.strip().splitlines()
    if len(lines) < 2:
        return None
    start_match = _START_RE.fullmatch(lines[0])
    end_match = _END_RE.fullmatch(lines[-1])
    if start_match is None or end_match is None:
        return None
    attrs = {
        key.lower(): value
        for key, value in _ATTR_RE.findall(start_match.group("attrs"))
    }
    table_id = attrs.get("id", "").strip()
    if not table_id or end_match.group("id") != table_id:
        return None
    return "\n".join(lines[1:-1]).strip()


__all__ = ["extract_linkparse_table_body"]
