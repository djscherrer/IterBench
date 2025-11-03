import logging
import os
import pathlib
import sys
import threading
from io import StringIO
from typing import Any

from pydantic import SecretStr
from openhands.sdk import LLM, Conversation, Event, Agent, Tool
from openhands.sdk.context.condenser import LLMSummarizingCondenser
from openhands.tools.preset.default import register_default_tools
from ansi2html import Ansi2HTMLConverter
from contextlib import redirect_stdout, redirect_stderr

from env.base import Env
from scenarios.base import Scenario
from prompts import KeyLocs

_agent_creation_lock = threading.Lock()
_tools_registered = False

class OpenHandsPrompter:
    def __init__(
        self,
        env: Env,
        scenario: Scenario,
        model: str,
        spec_type: str,
        safety_prompt: str,
        temperature: float,
        provider: str | None,
        max_cost: float | None,
        max_tokens: int | None,
        max_iterations: int,
        agent_cls: str,
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
        self.provider = provider

        self.task = self.scenario.build_prompt(
            self.env, self.spec_type, self.safety_prompt, agent=True
        )

    def _get_llm_params(self) -> tuple[str, str, str | None]:
        provider_config = {
            "swissai": {
                "prefix": "openai",
                "base_url": "https://api.swissai.cscs.ch/v1",
                "api_key": KeyLocs.cscs_key,
            },
            "openrouter": {
                "prefix": "openrouter",
                "base_url": None,
                "api_key": KeyLocs.openrouter_key,
            },
            "anthropic": {
                "prefix": "anthropic",
                "base_url": None,
                "api_key": KeyLocs.anthropic_key,
            },
            "together_ai": {
                "prefix": "together_ai",
                "base_url": None,
                "api_key": KeyLocs.together_key,
            },
            "openai": {
                "prefix": None,
                "base_url": None,
                "api_key": KeyLocs.openai_key,
            },
        }
        
        if self.provider is None:
            provider_prefix = self.model.split("/")[0]
            if provider_prefix in provider_config:
                config = provider_config[provider_prefix]
                if config["prefix"] and not self.model.startswith(f"{config['prefix']}/"):
                    model_name = f"{config['prefix']}{self.model[len(provider_prefix): ]}"
                else:
                    model_name = self.model
                return (model_name, os.environ[config["api_key"].value], config["base_url"])
            else:
                raise ValueError(f"Cannot infer provider from model name: {self.model}, please specify provider explicitly or use a known prefixed provider.")

        if self.provider == "vllm":
            # TODO: implement OpenHands support for vLLM
            raise ValueError("OpenHands does not support vLLM yet")


        if self.provider not in provider_config:
            raise ValueError(f"Unknown provider: {self.provider}")

        config = provider_config[self.provider]
        prefix = config["prefix"]
        base_url = config["base_url"]
        api_key = os.environ[config["api_key"].value]

        if prefix and not self.model.startswith(f"{prefix}/"):
            model_name = f"{prefix}/{self.model}"
        else:
            model_name = self.model

        return (model_name, api_key, base_url)

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

        # log file for OpenHands output
        openhands_log_file = save_dir / f"sample{sample_id}" / "openhands.log"
        openhands_console_log_file = save_dir / f"sample{sample_id}" / "openhands_console.html"
        openhands_log_file.parent.mkdir(parents=True, exist_ok=True)

        model_name, api_key, base_url = self._get_llm_params()

        # list to capture all events for logging
        events: list[Event] = []
        limits = {"exceeded": False, "reason": ""}
        conversation: Conversation | None = None
        
        def event_callback(event: Event) -> None:
            events.append(event)
            
            if len(events) > self.max_iterations:
                limits["exceeded"] = True
                limits["reason"] = f"Iteration limit exceeded: {len(events)} > {self.max_iterations}"
                logger.warning(limits["reason"])
                if conversation is not None:
                    conversation.pause()
                return
            
            if llm.metrics is not None:
                if self.max_cost is not None and llm.metrics.accumulated_cost > self.max_cost:
                    limits["exceeded"] = True
                    limits["reason"] =  f"Cost limit exceeded: ${llm.metrics.accumulated_cost:.4f} > ${self.max_cost:.4f}"
                    logger.warning(limits["reason"])
                    if conversation is not None:
                        conversation.pause()
                
                if self.max_tokens is not None and llm.metrics.accumulated_token_usage is not None:
                    total_tokens = (
                        llm.metrics.accumulated_token_usage.prompt_tokens +
                        llm.metrics.accumulated_token_usage.completion_tokens
                    )
                    if total_tokens > self.max_tokens:
                        limits["exceeded"] = True
                        limits["reason"] =  f"Token limit exceeded: {total_tokens} > {self.max_tokens}"
                        logger.warning(limits["reason"])
                        if conversation is not None:
                            conversation.pause()

        try:
            with _agent_creation_lock:
                global _tools_registered
                if not _tools_registered:
                    register_default_tools(enable_browser=False)
                    _tools_registered = True
                    
            # capture stdout and stderr and print to file
            console_output = StringIO()

            with redirect_stdout(console_output), redirect_stderr(console_output):
                llm = LLM(
                    model=model_name,
                    api_key=SecretStr(api_key),
                    base_url=base_url,
                    usage_id="baxbench",
                )

                with _agent_creation_lock:
                    tools = [
                        Tool(name="BashTool"),
                        Tool(name="FileEditorTool"),
                        Tool(name="TaskTrackerTool"),
                    ]
                    
                    condenser = LLMSummarizingCondenser(
                        llm=llm.model_copy(update={"usage_id": "condenser"}), 
                        max_size=80, 
                        keep_first=4
                    )
                    
                    agent = Agent(
                        llm=llm,
                        tools=tools,
                        mcp_config={}, 
                        system_prompt_kwargs={"cli_mode": True},
                        condenser=condenser,
                        security_analyzer=None,
                    )

                    conversation = Conversation(
                        agent=agent, 
                        workspace=str(code_dir),
                        callbacks=[event_callback],
                    )
                
                conversation.send_message(self.task)
                conversation.run()
    
            # convert ANSI output to HTML
            converter = Ansi2HTMLConverter()
            html_output = converter.convert(console_output.getvalue(), full=True)
            
            with open(openhands_console_log_file, 'w') as html_log:
                html_log.write(html_output)
            
            if limits["reason"]:
                logger.warning(f"Agent execution stopped: {limits['reason']}")

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