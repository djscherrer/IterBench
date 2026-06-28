"""Parse LLM code-generation responses into a file map."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from llm import Parser


def parse_code_response(
    raw_response: str,
    *,
    env: Any,
    logger: logging.Logger,
) -> dict[Path, str]:
    """Parse ``<FILEPATH>`` / ``<CODE>`` blocks from an LLM response."""
    files = Parser(env, logger).parse_response(raw_response)
    if Path("failed") in files:
        raise ValueError(
            "parse failure (LLM response did not contain expected code blocks)"
        )
    return files
