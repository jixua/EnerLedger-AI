from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JENKINSFILE = ROOT / "deploy" / "jenkins" / "Jenkinsfile.production"


def test_production_pipeline_only_streams_missing_or_changed_images() -> None:
    pipeline = JENKINSFILE.read_text(encoding="utf-8")

    assert "docker image inspect --format '{{.Id}}'" in pipeline
    assert 'actual_id="$(docker image inspect --format "{{.Id}}"' in pipeline
    assert 'if [ "$actual_id" != "$expected_id" ]; then' in pipeline
    assert 'xargs docker save <"$changed_images"' in pipeline
    assert 'xargs docker save <"$image_list"' not in pipeline


def test_production_pipeline_skips_transfer_when_all_images_match() -> None:
    pipeline = JENKINSFILE.read_text(encoding="utf-8")

    assert 'if [ ! -s "$changed_images" ]; then' in pipeline
    assert "Production already has all required images; transfer skipped." in pipeline
