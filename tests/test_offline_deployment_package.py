from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
OFFLINE = ROOT / "deploy" / "offline"


def test_offline_compose_uses_archived_images_without_build_context() -> None:
    compose = yaml.safe_load((OFFLINE / "docker-compose.yml").read_text())
    expected = {
        "mysql",
        "minio",
        "minio-init",
        "qdrant",
        "manticore",
        "rabbitmq",
        "pi-agent",
        "api",
        "parse-worker",
        "report-worker",
        "frontend",
    }

    assert set(compose["services"]) == expected
    assert all("image" in service for service in compose["services"].values())
    assert all("build" not in service for service in compose["services"].values())
    assert compose["services"]["report-worker"]["profiles"] == ["reports"]


def test_business_datastores_use_named_persistent_volumes() -> None:
    compose_text = (OFFLINE / "docker-compose.yml").read_text()

    for suffix in (
        "mysql-data",
        "minio-data",
        "qdrant-data",
        "manticore-data",
        "rabbitmq-data",
    ):
        assert f"${{COMPOSE_PROJECT_NAME:-enerledger-offline}}_{suffix}" in compose_text


def test_restore_refuses_existing_volumes_and_placeholder_secrets() -> None:
    restore = (OFFLINE / "scripts" / "restore-data.sh").read_text()

    assert "--confirm-empty-target" in restore
    assert "grep -q 'CHANGE_ME'" in restore
    assert "docker volume inspect" in restore
    assert "拒绝覆盖" in restore


def test_export_encrypts_environment_and_excludes_rabbitmq_data() -> None:
    export = (OFFLINE / "scripts" / "export-dev-package.sh").read_text()

    assert "openssl enc -aes-256-cbc -pbkdf2 -salt" in export
    assert "PACKAGE_PASSPHRASE_FILE" in export
    assert "initial-reviewer-password.txt.enc" in export
    assert "rabbitmq-data.tar.gz" not in export
    assert "mysqldump" in export
    assert "--single-transaction" in export
    assert "docker image save" in export


def test_package_documents_required_data_boundaries() -> None:
    migration_doc = (OFFLINE / "docs" / "数据迁移手册.md").read_text()

    assert "MySQL" in migration_doc
    assert "MinIO" in migration_doc
    assert "Qdrant" in migration_doc
    assert "Manticore" in migration_doc
    assert "RabbitMQ：不迁移瞬时队列消息" in migration_doc
    assert "API_KEY_ENCRYPTION_SECRET" in migration_doc
    assert "不得使用 `alembic stamp`" in migration_doc
