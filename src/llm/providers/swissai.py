"""SwissAI (CSCS) provider adapter."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from openai import OpenAI

from ..config import OPENAI_TOGETHER_CONTEXT_LENGTHS
from ..keys import KeyLocs
from ._base import single_completion

if TYPE_CHECKING:
    from ..prompter import Prompter


def prompt_swissai(prompter: Prompter, logger: logging.Logger) -> list[str]:
    client = OpenAI(
        api_key=os.environ[KeyLocs.cscs_key.value],
        base_url="https://api.swissai.cscs.ch/v1",
    )
    _, content = single_completion(
        prompter,
        logger,
        client=client,
        provider_label="swissai",
        context_lengths=OPENAI_TOGETHER_CONTEXT_LENGTHS,
    )
    return [content]
