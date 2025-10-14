import logging
import os
import pathlib
import subprocess
import sys
from typing import Any

from env.base import Env
from scenarios.base import Scenario
from prompts import KeyLocs


class OpenHandsPrompter:
    def __init__(
        self,
        env: Env,
        scenario: Scenario,
        model: str,
        spec_type: str,
        safety_prompt: str,
        temperature: float,
        openrouter: bool,
    ):
        self.env = env
        self.scenario = scenario
        self.model = model
        self.spec_type = spec_type
        self.safety_prompt = safety_prompt
        self.temperature = temperature
        self.openrouter = openrouter

        self.anthropic = "claude" in model
        self.openai = "gpt" in model or model.startswith("o1") or model.startswith("o3")

        self.task = self.scenario.build_prompt(
            self.env, self.spec_type, self.safety_prompt, agent=True
        )

    def get_code_dir(self, save_dir: pathlib.Path, sample: int) -> pathlib.Path:
        return save_dir / f"sample{sample}" / "code"

    def generate_code_with_agent(
        self,
        sample_id: int,
        save_dir: pathlib.Path,
        logger: logging.Logger,
    ) -> pathlib.Path:
        code_dir = self.get_code_dir(save_dir, sample_id)
        code_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Starting OpenHands agent for sample {sample_id}")
        logger.info(f"Code directory: {code_dir}")
        logger.info(f"Task:\n{self.task}")
        logger.info("-" * 80)

        if self.anthropic:
            provider = "anthropic"
            api_key = os.environ[KeyLocs.anthropic_key.value]
        elif self.openrouter:
            provider = "openrouter"
            api_key = os.environ[KeyLocs.openrouter_key.value]
        else:
            provider = "openai"
            api_key = os.environ[KeyLocs.openai_key.value]

        env = os.environ.copy()
        env.update({
            "RUNTIME": "local",
            "LLM_PROVIDER": provider,
            "LLM_MODEL": self.model,
            "LLM_API_KEY": api_key,
            "ENABLE_BROWSER": "false",
            "AGENT_ENABLE_BROWSING": "false",
            "SANDBOX_VOLUMES": f"{code_dir}:/workspace:rw",
        })

        try:
            logger.info("Running OpenHands agent...")

            # TODO: is there a better maybe async way to run this?
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "openhands.core.main",
                    "-t", self.task,
                    "-d", str(code_dir),
                    "-i", str(30),
                ],
                check=True,
                env=env,
                capture_output=True,
                text=True,
            )

            logger.info("OpenHands stdout:\n%s", result.stdout)
            if result.stderr:
                logger.warning("OpenHands stderr:\n%s", result.stderr)

            logger.info(f"Agent completed successfully! Generated code saved to {code_dir}")
            logger.info("-" * 80)

            return code_dir
        
        except Exception as e:
            logger.exception(f"OpenHands agent failed: {e}", exc_info=e)
            raise