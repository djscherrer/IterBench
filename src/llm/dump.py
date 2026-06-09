"""Outgoing prompt dump for manual inspection."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .cache import anthropic_messages, anthropic_system_param
from .messages import provider_messages

if TYPE_CHECKING:
    from .prompter import Prompter


def prompt_log_dir() -> pathlib.Path:
    """
    Top-level directory (sibling of ``results/``) where every outgoing prompt
    is dumped. Override with ``BAXBENCH_PROMPT_LOG_DIR``.
    """
    override = os.environ.get("BAXBENCH_PROMPT_LOG_DIR")
    if override:
        return pathlib.Path(override)
    return pathlib.Path(__file__).resolve().parent.parent.parent / "prompts"


def dump_outgoing_prompt(prompter: Prompter, logger: logging.Logger) -> None:
    """
    Dump the exact request payload about to be sent to the provider.

    Best-effort: a dump failure must never break an actual LLM call.
    """
    try:
        dump_dir = prompt_log_dir()
        dump_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        ts = now.strftime("%Y%m%d-%H%M%S-%f")
        model_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(prompter.model))
        stem = f"{ts}_pid{os.getpid()}_{prompter.provider}_{model_slug}"

        if prompter.provider == "anthropic":
            messages = anthropic_messages(prompter)
            sends_system = (not prompter.anthropic_thinking) or prompter.conversational
            out_of_band_system = (
                anthropic_system_param(prompter) if sends_system else None
            )
        else:
            messages = provider_messages(prompter)
            out_of_band_system = None

        payload = {
            "provider": prompter.provider,
            "model": prompter.model,
            "temperature": getattr(prompter, "temperature", None),
            "system_out_of_band": out_of_band_system,
            "messages": messages,
        }
        (dump_dir / f"{stem}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        def _render(content: Any) -> str:
            if isinstance(content, str):
                return content
            return json.dumps(content, indent=2, ensure_ascii=False)

        lines: list[str] = [
            f"# timestamp: {now.isoformat()}",
            f"# provider: {prompter.provider}",
            f"# model: {prompter.model}",
            f"# temperature: {getattr(prompter, 'temperature', None)}",
            f"# conversational: {getattr(prompter, 'conversational', False)}",
            f"# messages: {len(messages)}",
        ]
        if out_of_band_system is not None:
            lines += [
                "",
                "----- system (out-of-band) -----",
                _render(out_of_band_system),
            ]
        for i, msg in enumerate(messages, start=1):
            lines.append("")
            lines.append(f"----- message {i:03d} [{msg.get('role', '')}] -----")
            lines.append(_render(msg.get("content", "")))
        (dump_dir / f"{stem}.txt").write_text(
            "\n".join(lines), encoding="utf-8"
        )
        logger.info("Wrote outgoing prompt dump to %s.{json,txt}", dump_dir / stem)
    except Exception as exc:
        logger.warning("Could not write outgoing prompt dump: %s", exc)
