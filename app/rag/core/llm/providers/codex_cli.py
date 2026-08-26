"""Codex CLI text provider for local development mode."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from collections.abc import AsyncIterator
from pathlib import Path

from app.rag.config import settings
from app.rag.core.llm.base_provider import BaseProvider
from app.rag.core.llm.exceptions import ProviderConnectionError
from app.rag.core.llm.interfaces import CapabilityType
from app.rag.core.llm.response import GenerateResult, StreamChunk, UsageInfo

_LOCAL_CHAT_INSTRUCTIONS = """你是能碳会计 AI 智能体的对话生成模型。
只根据下面给出的系统要求和用户输入生成最终回答。不要读取本机文件、不要调用工具、
不要修改任何内容，也不要解释 Codex 的执行过程。直接输出给最终用户的回答正文。"""
_CODEX_CLI_SEMAPHORE = asyncio.Semaphore(settings.CODEX_CLI_MAX_CONCURRENCY)


class CodexCliProvider(BaseProvider):
    """Run a one-shot, ephemeral Codex CLI process for each chat request."""

    def __init__(
        self,
        provider_type: str = "codex_cli",
        provider_name: str = "codex_cli",
        api_key: str = "",
        api_base_url: str | None = None,
        model_name: str | None = None,
        timeout_ms: int = 300000,
        **kwargs,
    ) -> None:
        super().__init__(
            provider_type=provider_type,
            provider_name=provider_name,
            api_key=api_key,
            api_base_url=api_base_url,
            timeout_ms=timeout_ms,
            max_retries=0,
            **kwargs,
        )
        self.model_name = model_name or "gpt-5.4-mini"
        self._capabilities = {CapabilityType.TEXT}

    @staticmethod
    def _build_prompt(prompt: str, system_prompt: str | None) -> str:
        parts = [_LOCAL_CHAT_INSTRUCTIONS]
        if system_prompt:
            parts.extend(("\n<system_requirements>", system_prompt, "</system_requirements>"))
        parts.extend(("\n<user_input>", prompt, "</user_input>"))
        return "\n".join(parts)

    def _command(self, workdir: Path) -> list[str]:
        executable = shutil.which(settings.CODEX_CLI_PATH)
        if executable is None:
            raise ProviderConnectionError(
                message=f"Codex CLI executable not found: {settings.CODEX_CLI_PATH}",
                provider_type=self.provider_type,
            )
        command = [
            executable,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            self.model_name,
            "--json",
            "--cd",
            str(workdir),
            "--config",
            f'model_reasoning_effort="{settings.CODEX_CLI_REASONING_EFFORT}"',
        ]
        command.append("-")
        return command

    async def _run(self, prompt: str, system_prompt: str | None) -> GenerateResult:
        workdir = Path(settings.CODEX_CLI_WORKDIR).expanduser()
        workdir.mkdir(parents=True, exist_ok=True)
        command = self._command(workdir)
        started_at = time.monotonic()

        async with _CODEX_CLI_SEMAPHORE:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
            )
            try:
                stdout, _stderr = await asyncio.wait_for(
                    process.communicate(self._build_prompt(prompt, system_prompt).encode("utf-8")),
                    timeout=self.timeout_ms / 1000,
                )
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                raise ProviderConnectionError(
                    message="Codex CLI request timeout",
                    provider_type=self.provider_type,
                ) from exc
            except asyncio.CancelledError:
                process.kill()
                await process.wait()
                raise

        if process.returncode != 0:
            raise ProviderConnectionError(
                message=(
                    f"Codex CLI exited with status {process.returncode}; "
                    "check the local CLI login and model availability"
                ),
                provider_type=self.provider_type,
            )

        content = ""
        usage = UsageInfo()
        for raw_line in stdout.decode("utf-8", errors="replace").splitlines():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    content = item["text"]
            elif event.get("type") == "turn.completed":
                raw_usage = event.get("usage") or {}
                input_tokens = int(raw_usage.get("input_tokens") or 0)
                output_tokens = int(raw_usage.get("output_tokens") or 0)
                usage = UsageInfo(
                    prompt_tokens=input_tokens,
                    completion_tokens=output_tokens,
                    total_tokens=int(raw_usage.get("total_tokens") or 0)
                    or input_tokens + output_tokens,
                )

        content = content.strip()
        if not content:
            raise ProviderConnectionError(
                message="Codex CLI completed without an agent response",
                provider_type=self.provider_type,
            )
        return GenerateResult(
            content=content,
            model=self.model_name,
            usage=usage,
            provider_type=self.provider_type,
            latency_ms=int((time.monotonic() - started_at) * 1000),
            finish_reason="stop",
        )

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs,
    ) -> GenerateResult:
        del temperature, max_tokens, kwargs
        return await self._run(prompt, system_prompt)

    async def stream(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        result = await self.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        yield StreamChunk(
            delta=result.content,
            content=result.content,
            is_end=True,
            usage=result.usage,
        )
