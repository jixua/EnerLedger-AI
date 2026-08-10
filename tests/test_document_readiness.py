from __future__ import annotations

from sqlalchemy.dialects import mysql

from app.rag.core.pipeline.recall.document_readiness import MySqlDocumentReadinessGate


def test_pdf_and_word_recall_gate_require_scalar_and_json_quality_status() -> None:
    statement = MySqlDocumentReadinessGate._build_query(["chunk-1"], user_id=7)
    sql = str(
        statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()

    assert "document.parse_quality_status = 'passed'" in sql
    assert "json_extract(document.parse_quality, '$.status')" in sql
    assert "json_unquote" in sql
    assert "not in ('pdf', 'doc', 'docx')" in sql
