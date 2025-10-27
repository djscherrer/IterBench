import logging
import os
import pathlib
import subprocess
import sys
from typing import Any

from env.base import Env
from scenarios.base import Scenario
from prompts import KeyLocs, ProviderDetector


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
        explicit_provider: str | None = None,
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

        provider_name, api_key_loc = ProviderDetector.detect_provider(
            model, explicit_provider, openrouter, vllm=False
        )

        self.provider = provider_name
        self.api_key_location = api_key_loc
        self.anthropic = provider_name == "anthropic"
        self.openai = provider_name == "openai"
        self.together = provider_name == "together"

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

        if self.provider == "vllm":
            raise ValueError("OpenHands does not support vLLM yet")

        # SwissAI uses OpenAI-compatible API, so we set provider as "openai" with custom base URL
        if self.provider == "swissai":
            provider = "openai"
            base_url = "https://api.swissai.cscs.ch/v1"
            # Prefix model with "openai/" for LiteLLM (backend used by OpenHands) to recognize it
            model_name = f"openai/{self.model}" if not self.model.startswith("openai/") else self.model
        else:
            provider = self.provider
            base_url = None
            model_name = self.model

        api_key = os.environ[self.api_key_location.value]

        env = os.environ.copy()
        env_config = {
            "RUNTIME": "local",
            "LLM_PROVIDER": provider,
            "LLM_MODEL": model_name,
            "LLM_API_KEY": api_key,
            "ENABLE_BROWSER": "false",
            "AGENT_ENABLE_BROWSING": "false",
            "SANDBOX_VOLUMES": f"{code_dir}:/workspace:rw",
            "LOG_ALL_EVENTS": "true",
            "PORT": str(self.env.port),
        }

        if base_url:
            env_config["LLM_BASE_URL"] = base_url

        env.update(env_config)

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