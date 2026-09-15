from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
