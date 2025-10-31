import logging
import os
import pathlib
from typing import Any

from pydantic import SecretStr
from openhands.sdk import LLM, Conversation, Event
from openhands.tools.preset.default import get_default_agent

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
        agent_cls: str = "CodeActAgent",
        max_iterations: int = 30,
        explicit_provider: str | None = None,
        max_cost: float | None = None,
        max_tokens: int | None = None,
    ):
        self.env = env
        self.scenario = scenario
        self.model = model
        self.spec_type = spec_type
        self.safety_prompt = safety_prompt
        self.temperature = temperature
        self.agent_cls = agent_cls
        self.max_iterations = max_iterations
        self.max_cost = max_cost
        self.max_tokens = max_tokens

        provider_name, api_key_loc = ProviderDetector.get_provider(
            model, explicit_provider
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

        if self.provider == "vllm":
            raise ValueError("OpenHands does not support vLLM yet")

        # LiteLLM (backend used by OpenHands) requires provider prefix 
        # TODO: would be nice to use LiteLLM backend also for default genertion so to standardize everything 
        provider_prefixes = {
            "swissai": "openai",  # swissai uses openai-compatible api
            "openrouter": "openrouter",
            "anthropic": "anthropic",
            "together_ai": "together_ai",
            "openai": None,  
        }

        if self.provider == "swissai":
            base_url = "https://api.swissai.cscs.ch/v1"
        else:
            base_url = None

        prefix = provider_prefixes.get(self.provider)
        if prefix and not self.model.startswith(f"{prefix}/"):
            model_name = f"{prefix}/{self.model}"
        else:
            model_name = self.model

        api_key = os.environ[self.api_key_location.value]

        # Create list to capture all events for logging
        events: list[Event] = []
        limits = {"exceeded": False, "reason": ""}
        conversation_ref: Conversation | None = None
        
        def event_callback(event: Event) -> None:
            events.append(event)
            
            if len(events) > self.max_iterations:
                limits["exceeded"] = True
                limits["reason"] = f"Iteration limit exceeded: {len(events)} > {self.max_iterations}"
                logger.warning(limits["reason"])
                if conversation_ref is not None:
                    conversation_ref.pause()
                return
            
            if llm.metrics is not None:
                if self.max_cost is not None and llm.metrics.accumulated_cost > self.max_cost:
                    limits["exceeded"] = True
                    limits["reason"] =  f"Cost limit exceeded: ${llm.metrics.accumulated_cost:.4f} > ${self.max_cost:.4f}"
                    logger.warning(limits["reason"])
                    if conversation_ref is not None:
                        conversation_ref.pause()
                
                if self.max_tokens is not None and llm.metrics.accumulated_token_usage is not None:
                    total_tokens = (
                        llm.metrics.accumulated_token_usage.prompt_tokens +
                        llm.metrics.accumulated_token_usage.completion_tokens
                    )
                    if total_tokens > self.max_tokens:
                        limits["exceeded"] = True
                        limits["reason"] =  f"Token limit exceeded: {total_tokens} > {self.max_tokens}"
                        logger.warning(limits["reason"])
                        if conversation_ref is not None:
                            conversation_ref.pause()

        try:
            llm = LLM(
                model=model_name,
                api_key=SecretStr(api_key),
                base_url=base_url,
                usage_id="baxbench",
            )

            agent = get_default_agent(llm=llm, cli_mode=True)

            conversation_ref = Conversation(
                agent=agent, 
                workspace=str(code_dir),
                callbacks=[event_callback],
            )

            # send task to agent
            conversation_ref.send_message(self.task)

            # execute the conversation
            conversation_ref.run()
            
            if limits["reason"]:
                logger.warning(f"Agent execution stopped: {limits["reason"]}")

            # Write log to openhands.log
            with open(openhands_log_file, 'w') as log_file:
                log_file.write(f"OpenHands Agent Execution Log\n")
                log_file.write("=" * 80 + "\n\n")
                log_file.write(f"Task: {self.task}\n")
                log_file.write("=" * 80 + "\n")
                log_file.write(f"Model: {model_name}\n")
                log_file.write(f"Workspace: {code_dir}\n")
                log_file.write(f"Total Events: {len(events)}\n")
                
                if self.max_iterations is not None and self.max_cost is not None or self.max_tokens is not None:
                    log_file.write(f"\nLimits Configuration:\n")                    
                    log_file.write(f"  Max Iterations: {self.max_iterations}\n")
                    if self.max_cost is not None:
                        log_file.write(f"  Max Cost: ${self.max_cost:.4f}\n")
                    if self.max_tokens is not None:
                        log_file.write(f"  Max Tokens: {self.max_tokens}\n")
                
                if llm.metrics is not None:
                    log_file.write(f"\nFinal Metrics:\n")
                    log_file.write(f"  Cost: ${llm.metrics.accumulated_cost:.4f}\n")
                    if llm.metrics.accumulated_token_usage is not None:
                        log_file.write(f"  Prompt Tokens: {llm.metrics.accumulated_token_usage.prompt_tokens}\n")
                        log_file.write(f"  Completion Tokens: {llm.metrics.accumulated_token_usage.completion_tokens}\n")
                        total_tokens = (
                            llm.metrics.accumulated_token_usage.prompt_tokens +
                            llm.metrics.accumulated_token_usage.completion_tokens
                        )
                        log_file.write(f"  Total Tokens: {total_tokens}\n")
            
                log_file.write("=" * 80 + "\n\n")
                
                for i, event in enumerate(events, 1):
                    event_type = type(event).__name__
                    timestamp = getattr(event, 'timestamp', 'N/A')
                    log_file.write(f"\n{'=' * 80}\n")
                    log_file.write(f"Event {i}/{len(events)}: {event_type}\n")
                    log_file.write(f"Timestamp: {timestamp}\n")
                    log_file.write(f"{'-' * 80}\n")
                    log_file.write(f"{event}\n")
                
                log_file.write(f"\n{'=' * 80}\n")
                if limits["exceeded"]:
                    log_file.write(f"Conversation stopped due to limit: {limits["reason"]}\n")
                else:
                    log_file.write("Conversation completed\n")

            return code_dir

        except Exception as e:
            logger.exception(f"OpenHands agent failed: {e}", exc_info=e)
            if openhands_log_file.exists():
                logger.error(f"Check the full log at: {openhands_log_file}")
            raise