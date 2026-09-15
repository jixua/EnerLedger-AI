from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JENKINSFILE = ROOT / "deploy" / "jenkins" / "Jenkinsfile.production"


def test_production_pipeline_builds_changed_components_locally() -> None:
    pipeline = JENKINSFILE.read_text(encoding="utf-8")

    assert "Select changed application images" in pipeline
    assert ".production-build-components" in pipeline
    assert "Build changed application images locally" in pipeline
    assert "docker save" not in pipeline
    assert "docker load" not in pipeline


def test_production_pipeline_preserves_component_revisions_when_paths_do_not_change() -> None:
    pipeline = JENKINSFILE.read_text(encoding="utf-8")

    assert '"$DEPLOY_ROOT/.deployment/${component}_sha"' in pipeline
    assert "API_RELEASE_SHA" in pipeline
    assert "PI_RELEASE_SHA" in pipeline
    assert "WEB_RELEASE_SHA" in pipeline


def test_production_pipeline_uses_posix_compatible_script_installation() -> None:
    pipeline = JENKINSFILE.read_text(encoding="utf-8")

    assert "scripts/{deploy,rollback,validate-env,verify}.sh" not in pipeline
    for script in ("deploy", "rollback", "validate-env", "verify"):
        assert f"deploy/production/scripts/{script}.sh" in pipeline
