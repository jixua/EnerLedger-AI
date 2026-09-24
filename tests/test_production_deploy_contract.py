from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_production_api_image_includes_lambdamart_models() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    pipeline = (ROOT / "deploy/jenkins/Jenkinsfile.production").read_text()

    assert "COPY models ./models" in dockerfile
    assert "models/" in pipeline


def test_pi_runtime_files_are_owned_by_runtime_user() -> None:
    dockerfile = (ROOT / "deploy/jenkins/Dockerfile.pi").read_text()

    assert (
        "COPY --from=build --chown=node:node /app/third_party/pi ./third_party/pi"
        in dockerfile
    )
    assert "COPY --chown=node:node apps/pi-service ./apps/pi-service" in dockerfile


def test_production_deploy_recreates_only_changed_application_services() -> None:
    script = (ROOT / "deploy/production/scripts/deploy.sh").read_text()

    assert "application_services=()" in script
    assert 'application_services+=(api parse-worker)' in script
    assert 'application_services+=(pi-agent)' in script
    assert 'application_services+=(web)' in script
    assert 'application_services+=(report-worker)' in script
    assert "--force-recreate" in script
    assert '"${application_services[@]}"' in script


def test_production_deploy_does_not_force_recreate_stateful_services() -> None:
    script = (ROOT / "deploy/production/scripts/deploy.sh").read_text()

    recreate_block = script.split("application_services=(", 1)[1].split(
        "wait_for_services", 1
    )[0]
    for service in ("mysql", "minio", "qdrant", "manticore", "rabbitmq"):
        assert service not in recreate_block
