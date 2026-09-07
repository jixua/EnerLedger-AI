from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("path", "manifest_copy", "nltk_install_marker", "application_copy"),
    (
        (
            "Dockerfile",
            "COPY pyproject.toml uv.lock ./",
            "python -m nltk.downloader",
            "COPY app ./app",
        ),
        (
            "deploy/jenkins/Dockerfile.api",
            "COPY source/pyproject.toml source/uv.lock ./",
            'RUN mkdir -p "${NLTK_DATA}/tokenizers"',
            "COPY source/app ./app",
        ),
    ),
)
def test_api_dockerfile_keeps_dependency_layers_before_application_source(
    path: str,
    manifest_copy: str,
    nltk_install_marker: str,
    application_copy: str,
) -> None:
    dockerfile = (ROOT / path).read_text()

    dependency_manifest = dockerfile.index(manifest_copy)
    dependency_install = dockerfile.index("uv pip install", dependency_manifest)
    nltk_install = dockerfile.index(nltk_install_marker)
    application_copy_position = dockerfile.index(application_copy)
    project_install = dockerfile.index(
        "uv pip install --system --link-mode=copy --no-deps .",
        application_copy_position,
    )

    assert dependency_manifest < dependency_install < application_copy_position
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


def test_jenkins_api_dockerfile_uses_only_required_reachable_nltk_assets() -> None:
    dockerfile = (ROOT / "deploy/jenkins/Dockerfile.api").read_text()

    required_assets = (
        "packages/tokenizers/punkt.zip",
        "packages/tokenizers/punkt_tab.zip",
        "packages/corpora/stopwords.zip",
        "packages/corpora/wordnet.zip",
    )
    for asset in required_assets:
        assert f"https://www.nltk.org/nltk_data/{asset}" in dockerfile

    assert dockerfile.count("--retry-all-errors") == len(required_assets)
    assert dockerfile.count("--max-time 900") == len(required_assets)
    assert "cdn.jsdelivr.net" not in dockerfile
    assert "raw.githubusercontent.com" not in dockerfile
    assert "gh-proxy.com" not in dockerfile
    assert "packages/corpora/omw-1.4.zip" not in dockerfile
    assert "/tmp/omw-1.4.zip" not in dockerfile


def test_jenkins_api_dockerfile_keeps_nltk_layer_independent_from_lockfile() -> None:
    dockerfile = (ROOT / "deploy/jenkins/Dockerfile.api").read_text()

    nltk_install = dockerfile.index('RUN mkdir -p "${NLTK_DATA}/tokenizers"')
    dependency_manifest = dockerfile.index("COPY source/pyproject.toml source/uv.lock ./")

    assert nltk_install < dependency_manifest


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
