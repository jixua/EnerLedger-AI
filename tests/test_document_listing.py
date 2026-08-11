from __future__ import annotations

from collections import deque

import pytest
from sqlalchemy.dialects import mysql

from app.api.documents import _load_ordered_documents
from app.domain.models import Document


class _FakeScalarResult:
    def __init__(self, values):
        self._values = values

    def all(self):
        return list(self._values)


class _FakeSession:
    def __init__(self, *scalar_lists):
        self.scalar_lists = deque(scalar_lists)
        self.statements = []

    async def scalars(self, statement):
        self.statements.append(statement)
        return _FakeScalarResult(self.scalar_lists.popleft())


@pytest.mark.asyncio
async def test_load_ordered_documents_sorts_ids_before_loading_wide_rows() -> None:
    newer = Document(id=9)
    older = Document(id=4)
    db = _FakeSession([9, 4], [older, newer])

    documents = await _load_ordered_documents(db, Document.user_id == 1)

    assert [document.id for document in documents] == [9, 4]
    first_sql = str(db.statements[0].compile(dialect=mysql.dialect()))
    second_sql = str(db.statements[1].compile(dialect=mysql.dialect()))
    assert first_sql.startswith("SELECT document.id ")
    assert "document.parse_quality" not in first_sql
    assert "ORDER BY document.created_at DESC, document.id DESC" in first_sql
    assert "WHERE document.id IN" in second_sql


@pytest.mark.asyncio
async def test_load_ordered_documents_skips_wide_query_when_empty() -> None:
    db = _FakeSession([])

    assert await _load_ordered_documents(db, Document.user_id == 1) == []
    assert len(db.statements) == 1
