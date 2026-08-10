from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.llm import _validate_protocol_capability
from app.rag.core.llm.encryption import decrypt_api_key, encrypt_api_key, mask_api_key


def test_api_key_is_encrypted_and_only_masked_for_display() -> None:
    plaintext = "test-api-key-value-00001234"

    ciphertext = encrypt_api_key(plaintext)

    assert plaintext not in ciphertext
    assert decrypt_api_key(ciphertext) == plaintext
    assert mask_api_key(plaintext) == "test****....****1234"


def test_protocol_capability_validation_uses_migrated_model_factory() -> None:
    _validate_protocol_capability("openai", "CHAT")
    _validate_protocol_capability("bge_m3", "SPARSE_EMBEDDING")
    _validate_protocol_capability("doubao_vision", "SPARSE_EMBEDDING")

    with pytest.raises(HTTPException) as exc_info:
        _validate_protocol_capability("bge_m3", "CHAT")

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "UNSUPPORTED_PROTOCOL_CAPABILITY"
