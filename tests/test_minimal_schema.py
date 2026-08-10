from __future__ import annotations

import os
import re
import runpy
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSIONS_DIR = PROJECT_ROOT / "migrations" / "versions"
CORE_TABLES = {"dataset", "document", "document_chunk", "llm_config"}


def test_importing_main_registers_exactly_the_four_core_tables() -> None:
    """用干净进程验证 app.main 的导入闭包，避免 pytest 收集其他模块污染 metadata。"""

    script = """
import app.main
from app.rag.models.db_models import Base

actual = set(Base.metadata.tables)
expected = {"dataset", "document", "document_chunk", "llm_config"}
if actual != expected:
    raise SystemExit(f"unexpected metadata tables: {sorted(actual)}")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_legacy_model_imports_cannot_expand_core_metadata() -> None:
    """上游遗留模型即使被误导入，也不能重新注册已删除的业务表。"""

    script = """
import app.main
import app.rag.models.dataset_parse_config
import app.rag.models.parse_task
import app.rag.models.wiki_tree
import app.rag.models.workflow
from app.rag.models.db_models import Base

actual = set(Base.metadata.tables)
expected = {"dataset", "document", "document_chunk", "llm_config"}
if actual != expected:
    raise SystemExit(f"unexpected metadata tables: {sorted(actual)}")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_alembic_has_minimal_root_and_document_queue_revision() -> None:
    version_files = sorted(path for path in VERSIONS_DIR.glob("*.py") if path.name != "__init__.py")
    assert [path.name for path in version_files] == [
        "0001_minimal_rag.py",
        "0002_document_parse_queue.py",
        "0003_chunk_structure_metadata.py",
    ]

    root_revision = runpy.run_path(str(version_files[0]))
    queue_revision = runpy.run_path(str(version_files[1]))
    chunk_metadata_revision = runpy.run_path(str(version_files[2]))
    assert root_revision["revision"] == "0001_minimal_rag"
    assert root_revision["down_revision"] is None
    assert queue_revision["revision"] == "0002_document_parse_queue"
    assert queue_revision["down_revision"] == "0001_minimal_rag"
    assert chunk_metadata_revision["revision"] == "0003_chunk_structure_metadata"
    assert chunk_metadata_revision["down_revision"] == "0002_document_parse_queue"


def test_alembic_offline_sql_contains_only_minimal_schema() -> None:
    env = os.environ.copy()
    env["ALEMBIC_DATABASE_URL"] = "mysql+pymysql://user:pass@localhost/minimal_rag_test"
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    sql = completed.stdout.lower()
    created_tables = {
        match.strip("`") for match in re.findall(r"create\s+table\s+(`?[a-z0-9_]+`?)", sql)
    }
    assert created_tables == CORE_TABLES | {"alembic_version"}

    legacy_tables = {
        "dataset_parse_config",
        "document_original_file",
        "document_parse_file",
        "document_parsed_log",
        "document_parse_pipeline",
        "document_post_process_pipeline",
        "chat_conversation",
        "chat_message",
        "llm_usage_log",
        "llm_system_provider",
        "llm_provider_model",
        "llm_model_config",
        "llm_capability_default",
        "wiki_tree_node",
    }
    assert all(table not in sql for table in legacy_tables)


def test_readable_sql_snapshot_contains_current_chunk_structure_column() -> None:
    sql = (PROJECT_ROOT / "migrations" / "db.sql").read_text(encoding="utf-8").lower()

    assert "structure_metadata json null" in sql
