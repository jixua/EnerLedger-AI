from __future__ import annotations

import os
import re
import runpy
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSIONS_DIR = PROJECT_ROOT / "migrations" / "versions"
CORE_TABLES = {
    "agent_conversation",
    "agent_conversation_turn",
    "dataset",
    "document",
    "document_chunk",
    "document_folder",
    "llm_config",
    "report_run",
    "report_question",
    "report_artifact",
    "structured_asset",
    "structured_asset_alias",
    "structured_asset_version",
    "structured_query_audit",
    "structured_table",
    "structured_term_alias",
}


def test_importing_main_registers_current_core_tables() -> None:
    """用干净进程验证 app.main 的导入闭包，避免 pytest 收集其他模块污染 metadata。"""

    script = """
import app.main
from app.rag.models.db_models import Base

actual = set(Base.metadata.tables)
expected = {
    "agent_conversation", "agent_conversation_turn",
    "dataset", "document", "document_chunk", "document_folder", "llm_config",
    "report_run", "report_question", "report_artifact",
    "structured_asset", "structured_asset_alias", "structured_asset_version",
    "structured_query_audit", "structured_table", "structured_term_alias",
}
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
expected = {
    "agent_conversation", "agent_conversation_turn",
    "dataset", "document", "document_chunk", "document_folder", "llm_config",
    "report_run", "report_question", "report_artifact",
    "structured_asset", "structured_asset_alias", "structured_asset_version",
    "structured_query_audit", "structured_table", "structured_term_alias",
}
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


def test_alembic_has_single_minimal_revision_chain() -> None:
    version_files = sorted(path for path in VERSIONS_DIR.glob("*.py") if path.name != "__init__.py")
    assert [path.name for path in version_files] == [
        "0001_minimal_rag.py",
        "0002_document_parse_queue.py",
        "0003_chunk_structure_metadata.py",
        "0004_document_parse_quality.py",
        "0005_dataset_vision_config.py",
        "0006_document_dispatch_outbox.py",
        "0007_crawler_document_review.py",
        "0007_document_filename_unique.py",
        "0007_document_folders.py",
        "0008_document_folder_hierarchy.py",
        "0009_report_platform_foundation.py",
        "0009_structured_assets.py",
        "0010_structured_report_merge.py",
        "0011_agent_conversations.py",
    ]

    root_revision = runpy.run_path(str(version_files[0]))
    queue_revision = runpy.run_path(str(version_files[1]))
    chunk_metadata_revision = runpy.run_path(str(version_files[2]))
    parse_quality_revision = runpy.run_path(str(version_files[3]))
    vision_config_revision = runpy.run_path(str(version_files[4]))
    dispatch_outbox_revision = runpy.run_path(str(version_files[5]))
    crawler_review_revision = runpy.run_path(str(version_files[6]))
    filename_unique_revision = runpy.run_path(str(version_files[7]))
    folders_revision = runpy.run_path(str(version_files[8]))
    folder_hierarchy_revision = runpy.run_path(str(version_files[9]))
    report_revision = runpy.run_path(str(version_files[10]))
    structured_assets_revision = runpy.run_path(str(version_files[11]))
    merge_revision = runpy.run_path(str(version_files[12]))
    conversation_revision = runpy.run_path(str(version_files[13]))
    assert root_revision["revision"] == "0001_minimal_rag"
    assert root_revision["down_revision"] is None
    assert queue_revision["revision"] == "0002_document_parse_queue"
    assert queue_revision["down_revision"] == "0001_minimal_rag"
    assert chunk_metadata_revision["revision"] == "0003_chunk_structure_metadata"
    assert chunk_metadata_revision["down_revision"] == "0002_document_parse_queue"
    assert parse_quality_revision["revision"] == "0004_document_parse_quality"
    assert parse_quality_revision["down_revision"] == "0003_chunk_structure_metadata"
    assert vision_config_revision["revision"] == "0005_dataset_vision_config"
    assert vision_config_revision["down_revision"] == "0004_document_parse_quality"
    assert dispatch_outbox_revision["revision"] == "0006_document_dispatch_outbox"
    assert dispatch_outbox_revision["down_revision"] == "0005_dataset_vision_config"
    assert crawler_review_revision["revision"] == "0007_crawler_document_review"
    assert crawler_review_revision["down_revision"] == "0006_document_dispatch_outbox"
    assert filename_unique_revision["revision"] == "0007_document_filename_unique"
    assert filename_unique_revision["down_revision"] == "0006_document_dispatch_outbox"
    assert folders_revision["revision"] == "0007_document_folders"
    assert folders_revision["down_revision"] == "0006_document_dispatch_outbox"
    assert folder_hierarchy_revision["revision"] == "0008_document_folder_hierarchy"
    assert folder_hierarchy_revision["down_revision"] == (
        "0007_crawler_document_review",
        "0007_document_folders",
    )
    assert report_revision["revision"] == "0009_report_platform_foundation"
    assert report_revision["down_revision"] == "0008_document_folder_hierarchy"
    assert structured_assets_revision["revision"] == "0009_structured_assets"
    assert structured_assets_revision["down_revision"] == (
        "0008_document_folder_hierarchy",
        "0007_document_filename_unique",
    )
    assert merge_revision["revision"] == "0010_structured_report_merge"
    assert merge_revision["down_revision"] == (
        "0009_report_platform_foundation",
        "0009_structured_assets",
    )
    assert conversation_revision["revision"] == "0011_agent_conversations"
    assert conversation_revision["down_revision"] == "0010_structured_report_merge"


def test_alembic_offline_sql_contains_only_minimal_schema() -> None:
    env = os.environ.copy()
    env["ALEMBIC_DATABASE_URL"] = "mysql+pymysql://user:pass@localhost/minimal_rag_test"
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "heads", "--sql"],
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
    assert "alter table document add column parse_quality_status varchar(32)" in sql
    assert "alter table document add column parse_quality json" in sql
    assert "legacy_unchecked" in sql
    assert "not_applicable" in sql
    assert "alter table dataset add column vision_config_id bigint unsigned" in sql
    assert "alter table document add column dispatch_status varchar(16)" in sql
    assert "alter table document add column review_status varchar(16)" in sql
    assert "alter table document add column source_metadata json" in sql
    assert "idx_document_review_status" in sql
    assert "create table document_folder" in sql
    assert "alter table document add column folder_id bigint unsigned" in sql
    assert "alter table document_folder add column parent_id bigint unsigned" in sql
    assert "idx_document_folder_parent" in sql
    assert "alter table llm_config add column supports_tool_calling bool" in sql
    assert "create table report_run" in sql
    assert "create table agent_conversation" in sql
    assert "create table agent_conversation_turn" in sql
    assert "custom_template_manifest json" in sql
    assert "template_snapshot json not null" in sql
    assert "model_snapshot json not null" in sql
    assert "document_manifest json not null" in sql
    assert "analysis_coverage json" in sql
    assert "uk_document_user_dataset_filename" in sql
    assert "create table structured_asset" in sql
    assert "create table structured_asset_version" in sql
    assert "create table structured_table" in sql
    assert "create table agent_conversation" in sql
    assert "create table agent_conversation_turn" in sql
    assert "create table structured_query_audit" in sql

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
    assert "parse_quality_status varchar(32) null" in sql
    assert "parse_quality json null" in sql
    assert "vision_config_id bigint unsigned null" in sql
    assert "dispatch_status varchar(16) not null" in sql
    assert "idx_document_dispatch_available" in sql
    assert "review_status varchar(16) not null" in sql
    assert "source_metadata json null" in sql
    assert "idx_document_review_status" in sql
    assert "create table document_folder" in sql
    assert "folder_id bigint unsigned null" in sql
    assert "idx_document_folder" in sql
    assert "parent_id bigint unsigned null" in sql
    assert "idx_document_folder_parent" in sql
    assert "template_snapshot json not null" in sql
    assert "document_manifest json not null" in sql
    assert "create table structured_asset" in sql
    assert "create table structured_asset_version" in sql
    assert "create table structured_table" in sql
