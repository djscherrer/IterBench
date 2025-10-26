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
        agent_cls: str = "CodeActAgent",
        max: int = 30,
        verbose: bool = False,
    ):
        self.env = env
        self.scenario = scenario
        self.model = model
        self.spec_type = spec_type
        self.safety_prompt = safety_prompt
        self.temperature = temperature
        self.openrouter = openrouter
        self.agent_cls = agent_cls
        self.max_iterations = max
        self.verbose = verbose

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

        # Create a log file for OpenHands output
        openhands_log_file = save_dir / f"sample{sample_id}" / "openhands.log"
        openhands_log_file.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"Starting OpenHands agent for sample {sample_id}")
        logger.info(f"Code directory: {code_dir}")
        logger.info(f"OpenHands log file: {openhands_log_file}")
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
            "LOG_ALL_EVENTS": "true",
            "PORT": str(self.env.port),
        })

        try:
            logger.info("Running OpenHands agent...")
            logger.info("=" * 80)

            # Open log file for writing
            with open(openhands_log_file, 'w') as log_file:
                # Run OpenHands with streaming output
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "openhands.core.main",
                        "-c", self.agent_cls,
                        "-t", self.task,
                        "-d", str(code_dir),
                        "-i", str(self.max_iterations),
                    ],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1, 
                )

                if process.stdout:
                    for line in process.stdout:

                        log_file.write(line)
                        log_file.flush()

                        if self.verbose:
                            print(line, end='', flush=True)

                return_code = process.wait()

                if return_code != 0:
                    raise subprocess.CalledProcessError(return_code, process.args)

            logger.info("=" * 80)
            logger.info(f"Agent completed successfully! Generated code saved to {code_dir}")
            logger.info(f"Full agent log saved to: {openhands_log_file}")
            logger.info("-" * 80)

            return code_dir
        
        except Exception as e:
            logger.exception(f"OpenHands agent failed: {e}", exc_info=e)
            if openhands_log_file.exists():
                logger.error(f"Check the full log at: {openhands_log_file}")
            raise