from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("path", "manifest_copy", "application_copy"),
    (
        ("Dockerfile", "COPY pyproject.toml uv.lock ./", "COPY app ./app"),
        (
            "deploy/jenkins/Dockerfile.api",
            "COPY source/pyproject.toml source/uv.lock ./",
            "COPY source/app ./app",
        ),
    ),
)
def test_api_dockerfile_keeps_dependency_layers_before_application_source(
    path: str,
    manifest_copy: str,
    application_copy: str,
) -> None:
    dockerfile = (ROOT / path).read_text()

    dependency_manifest = dockerfile.index(manifest_copy)
    dependency_install = dockerfile.index("uv pip install", dependency_manifest)
    nltk_install = dockerfile.index("NLTK_DATA", dependency_install)
    application_copy_position = dockerfile.index(application_copy)
    project_install = dockerfile.index(
        "uv pip install --system --link-mode=copy --no-deps .",
        application_copy_position,
    )

    assert dependency_manifest < dependency_install < nltk_install
    assert nltk_install < application_copy_position < project_install


@pytest.mark.parametrize("path", ("Dockerfile", "deploy/jenkins/Dockerfile.api"))
def test_api_dockerfile_uses_stable_locked_buildkit_caches(path: str) -> None:
    dockerfile = (ROOT / path).read_text()

    assert dockerfile.startswith("# syntax=docker/dockerfile:1.7\n")
    assert "uv export" in dockerfile
    assert "--frozen" in dockerfile
    assert "id=enerledger-pip,target=/root/.cache/pip,sharing=locked" in dockerfile
    assert dockerfile.count(
        "id=enerledger-uv,target=/root/.cache/uv,sharing=locked"
    ) == 2


def test_frontend_dockerfile_reuses_npm_download_cache() -> None:
    dockerfile = (ROOT / "deploy/jenkins/Dockerfile.frontend").read_text()

    assert dockerfile.startswith("# syntax=docker/dockerfile:1.7\n")
    assert "id=enerledger-npm,target=/root/.npm,sharing=locked" in dockerfile
    assert "npm ci --no-audit --no-fund" in dockerfile


def test_api_entrypoint_applies_every_candidate_migration_head() -> None:
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text()

    assert "alembic upgrade heads" in entrypoint
    assert "alembic upgrade head\n" not in entrypoint


@pytest.mark.parametrize(
    ("path", "reporting_copy"),
    (
        ("Dockerfile", "COPY reporting ./reporting"),
        ("deploy/jenkins/Dockerfile.api", "COPY source/reporting ./reporting"),
    ),
)
def test_api_images_include_report_template_assets(path: str, reporting_copy: str) -> None:
    dockerfile = (ROOT / path).read_text()

    assert reporting_copy in dockerfile
