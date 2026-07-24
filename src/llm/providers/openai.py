"""OpenAI (native) provider adapter."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from openai import OpenAI

from ..keys import KeyLocs
from ._base import batch_completion

if TYPE_CHECKING:
    from ..prompter import Prompter


def prompt_openai_batch(prompter: Prompter, logger: logging.Logger) -> list[str]:
    client = OpenAI(api_key=os.environ[KeyLocs.openai_key.value])
    return batch_completion(prompter, logger, client=client, provider_label="openai")
