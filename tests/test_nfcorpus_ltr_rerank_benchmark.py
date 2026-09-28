"""The shared rerank stage must use the model's complete candidate order."""

from types import SimpleNamespace

import pytest

from scripts.nfcorpus_ltr_rerank_benchmark import _rerank, _weknora_search


class FakeReranker:
    def __init__(self, indices: list[int]) -> None:
        self.indices = indices
        self.calls: list[tuple] = []

    async def rerank(self, query, documents, *, model, top_n):
        self.calls.append((query, documents, model, top_n))
        return SimpleNamespace(
            results=[SimpleNamespace(index=index, score=0.8) for index in self.indices]
        )


@pytest.mark.asyncio
async def test_rerank_uses_model_order_and_deduplicates_documents():
    model = FakeReranker([2, 0, 1])
    result = await _rerank(model, "question", [("a", "A"), ("b", "B"), ("a", "A2")])
    assert result == ["a", "b"]
    assert model.calls[0][1] == ["A", "B", "A2"]
    assert model.calls[0][3] == 3


@pytest.mark.asyncio
async def test_rerank_rejects_incomplete_model_output():
    model = FakeReranker([0, 0])
    with pytest.raises(ValueError, match="完整且唯一"):
        await _rerank(model, "question", [("a", "A"), ("b", "B")])


@pytest.mark.asyncio
async def test_weknora_search_retries_transient_server_error(monkeypatch):
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr("scripts.nfcorpus_ltr_rerank_benchmark.asyncio.sleep", no_wait)

    class Response:
        def __init__(self, status_code, hits=None):
            self.status_code = status_code
            self.hits = hits

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("server failed")

        def json(self):
            return {"success": True, "data": self.hits}

    class Client:
        def __init__(self):
            self.calls = 0

        async def post(self, _url, *, json):
            self.calls += 1
            assert json["match_count"] == 20
            return Response(500 if self.calls == 1 else 200, [{"id": "c"}])

    client = Client()
    assert await _weknora_search(client, "http://example", "kb", "query") == [{"id": "c"}]
    assert client.calls == 2
