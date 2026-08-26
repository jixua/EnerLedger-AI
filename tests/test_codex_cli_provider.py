from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.api.llm import _validate_protocol_capability
from app.domain.schemas import LLMConfigCreate
from app.rag.config import settings
from app.rag.core.llm.providers import codex_cli as codex_cli_module
from app.rag.core.llm.providers.codex_cli import CodexCliProvider
from app.rag.core.llm.runtime_config import RuntimeModelConfig
from app.rag.core.llm.user_model_resolver import build_provider_from_runtime_config


class _FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.input_bytes = b""
        self.killed = False

    async def communicate(self, input_bytes: bytes) -> tuple[bytes, bytes]:
        self.input_bytes = input_bytes
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


def test_codex_cli_config_requires_no_remote_credentials() -> None:
    config = LLMConfigCreate.model_validate(
        {
            "provider_type": "codex_cli",
            "model_name": "gpt-5.4-mini",
            "display_name": "本地 Codex 5.4 Mini",
            "capability": "CHAT",
            "protocol": "codex_cli",
        }
    )

    assert config.api_base_url is None
    assert config.api_key is None
    _validate_protocol_capability("codex_cli", "CHAT")

    with pytest.raises(ValidationError):
        LLMConfigCreate.model_validate(
            {
                "provider_type": "codex_cli",
                "model_name": "gpt-5.4-mini",
                "capability": "VISION",
                "protocol": "codex_cli",
            }
        )


def test_remote_config_still_requires_url_and_key() -> None:
    with pytest.raises(ValidationError):
        LLMConfigCreate.model_validate(
            {
                "provider_type": "deepseek",
                "model_name": "deepseek-v4-flash",
                "capability": "CHAT",
                "protocol": "openai",
            }
        )


def test_runtime_resolver_skips_api_key_decryption_for_codex_cli() -> None:
    resolved = build_provider_from_runtime_config(
        RuntimeModelConfig.model_validate(
            {
                "configId": 7,
                "scope": "USER",
                "ownerUserId": 1,
                "providerId": 1,
                "providerType": "codex_cli",
                "modelName": "gpt-5.4-mini",
                "displayName": "本地 Codex 5.4 Mini",
                "capability": "CHAT",
                "protocol": "codex_cli",
                "apiBaseUrl": "local://codex-cli",
                "apiKeyCiphertext": "local-no-api-key",
                "isActive": True,
                "snapshotVersion": 1,
            }
        ),
        capability="CHAT",
    )

    assert isinstance(resolved.provider, CodexCliProvider)
    assert resolved.model_name == "gpt-5.4-mini"


@pytest.mark.asyncio
async def test_codex_cli_provider_uses_ephemeral_medium_exec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    events = [
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "基于召回内容的回答。"},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 12, "cached_input_tokens": 3, "output_tokens": 8},
        },
    ]
    stdout = ("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n").encode()
    process = _FakeProcess(stdout)
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(codex_cli_module.shutil, "which", lambda _path: "/opt/bin/codex")
    monkeypatch.setattr(
        codex_cli_module.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(settings, "CODEX_CLI_WORKDIR", str(tmp_path / "codex-workdir"))
    monkeypatch.setattr(settings, "CODEX_CLI_REASONING_EFFORT", "medium")

    result = await CodexCliProvider(model_name="gpt-5.4-mini", timeout_ms=10000).generate(
        prompt="天然气燃烧排放如何核算？",
        system_prompt="仅根据召回片段回答。",
    )

    args = captured["args"]
    assert args[0:2] == ("/opt/bin/codex", "exec")
    assert "--ephemeral" in args
    assert "--ignore-user-config" in args
    assert ("--sandbox", "read-only") == args[args.index("--sandbox") : args.index("--sandbox") + 2]
    assert ("--model", "gpt-5.4-mini") == args[
        args.index("--model") : args.index("--model") + 2
    ]
    assert "fast_mode" not in args
    assert 'service_tier="fast"' not in args
    assert 'model_reasoning_effort="medium"' in args
    assert "仅根据召回片段回答。" in process.input_bytes.decode()
    assert result.content == "基于召回内容的回答。"
    assert result.usage.prompt_tokens == 12
    assert result.usage.completion_tokens == 8
    assert result.usage.total_tokens == 20

    chunks = [
        chunk
        async for chunk in CodexCliProvider(
            model_name="gpt-5.4-mini", timeout_ms=10000
        ).stream("测试")
    ]
    assert chunks[0].is_end is True
    assert chunks[0].delta == "基于召回内容的回答。"
