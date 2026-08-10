from app.rag.core.splitter.factory import _resolve_embed_batch_size


def test_qwen_37_embedding_batch_is_capped_at_official_limit() -> None:
    assert (
        _resolve_embed_batch_size(
            provider_type="qwen",
            model_name="qwen3.7-text-embedding",
            configured_batch_size=32,
        )
        == 20
    )


def test_qwen_37_embedding_keeps_smaller_operator_limit() -> None:
    assert (
        _resolve_embed_batch_size(
            provider_type="qwen",
            model_name="qwen3.7-text-embedding",
            configured_batch_size=8,
        )
        == 8
    )
