from pathlib import Path

import nltk

from app.rag.bootstrap.nltk_data import _resolve_nltk_data_dir


def test_default_nltk_data_dir_points_to_project_root(monkeypatch) -> None:
    monkeypatch.delenv("NLTK_DATA", raising=False)

    resolved = _resolve_nltk_data_dir()

    assert resolved == Path(__file__).resolve().parent.parent / "nltk_data"


def test_project_nltk_data_contains_punkt_tab(monkeypatch) -> None:
    monkeypatch.delenv("NLTK_DATA", raising=False)

    resolved = _resolve_nltk_data_dir()

    resource = nltk.data.find(
        "tokenizers/punkt_tab/english/",
        paths=[str(resolved)],
    )
    assert Path(resource).is_dir()
