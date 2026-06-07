import logging
import os
import pathlib
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from openhands.tools import (
    TerminalTool, 
    FileEditorTool, 
    TaskTrackerTool,
)

from ansi2html import Ansi2HTMLConverter
from openhands.sdk import LLM, Agent, Conversation, Event, Tool
from openhands.sdk.context.condenser import LLMSummarizingCondenser
from openhands.tools.preset.default import register_default_tools

# Now import OpenHands SDK (which will use our patched litellm)
from pydantic import SecretStr

# Patch OpenHands FileEditor to handle agent hallucinations about the file system.
# Specifically, map /root/ paths to the actual workspace root to prevent PermissionError.
try:
    from openhands.tools.file_editor.editor import FileEditor
    from pathlib import Path
    import logging

    _original_validate_path = FileEditor.validate_path

    def _patched_validate_path(self, command, path):
        path_str = str(path)
        if path_str.startswith('/root'):
            # Strip /root or /root/ prefix
            relative_part = path_str[5:].lstrip('/')
            # Map to current workspace root
            new_path = Path(self._cwd) / relative_part
            logging.getLogger("baxbench").info("Successfully patched OpenHands FileEditor to handle /root paths")
            return _original_validate_path(self, command, new_path)
        return _original_validate_path(self, command, path)

    FileEditor.validate_path = _patched_validate_path
except Exception as e:
    # Use standard print if logging is not yet configured
    print(f"Warning: Failed to patch OpenHands tools: {e}")

from db_manager import PostgresConnectionParams, PostgresManager
from env.base import Env
from env.templates import copy_template_to_workspace
from prompts import KeyLocs
from scenarios.base import Scenario

# from typing import Any
# from functools import wraps

# Import and patch litellm BEFORE OpenHands SDK imports
# import litellm

# # Apply the patch at module load time, before OpenHands imports
# _litellm_patched_at_import = False

# def _apply_openrouter_patch():
#     """
#     Patch litellm at module import time to inject OpenRouter provider routing.
#     This must happen before OpenHands SDK imports litellm.
#     """
#     global _litellm_patched_at_import
#     if _litellm_patched_at_import:
#         return

#     original_completion = litellm.completion
#     original_acompletion = litellm.acompletion

#     @wraps(original_completion)
#     def patched_completion(*args, **kwargs):
#         model = kwargs.get('model', '')
#         if isinstance(model, str) and model.startswith('openrouter/'):
#             extra_body = kwargs.get('extra_body', {})
#             if 'provider' not in extra_body:
#                 extra_body['provider'] = {}
#             # Specify providers known to support tool use for qwen models
#             extra_body['provider']['only'] = ['Together']
#             extra_body['provider']['order'] = ['Together']
#             extra_body['provider']['allow_fallbacks'] = False
#             extra_body['provider']['require_parameters'] = True
#             kwargs['extra_body'] = extra_body
#         return original_completion(*args, **kwargs)

#     @wraps(original_acompletion)
#     async def patched_acompletion(*args, **kwargs):
#         model = kwargs.get('model', '')
#         if isinstance(model, str) and model.startswith('openrouter/'):
#             extra_body = kwargs.get('extra_body', {})
#             if 'provider' not in extra_body:
#                 extra_body['provider'] = {}
#             extra_body['provider']['only'] = ['Together']
#             extra_body['provider']['order'] = ['Together']
#             extra_body['provider']['allow_fallbacks'] = False
#             extra_body['provider']['require_parameters'] = True
#             kwargs['extra_body'] = extra_body
#         return await original_acompletion(*args, **kwargs)

#     litellm.completion = patched_completion
#     litellm.acompletion = patched_acompletion
#     _litellm_patched_at_import = True

# # Apply patch immediately
# _apply_openrouter_patch()


_agent_creation_lock = threading.Lock()
_tools_registered = False
_anthropic_adaptive_thinking_patch_applied = False


def _patch_litellm_anthropic_adaptive_thinking(logger: logging.Logger) -> None:
    """Teach this LiteLLM/OpenHands combo that newer Claude Opus uses adaptive thinking."""
    global _anthropic_adaptive_thinking_patch_applied
    if _anthropic_adaptive_thinking_patch_applied:
        return

    try:
        from litellm.llms.anthropic.chat.transformation import AnthropicConfig
        from openhands.sdk.llm.utils.model_features import (
            _normalized_supported_openai_params,
        )

        def _is_adaptive_thinking_model(model: str) -> bool:
            model_lower = (model or "").lower()
            existing_detector = getattr(AnthropicConfig, "_is_claude_4_6_model", None)
            if existing_detector is not None and existing_detector(model_lower):
                return True

            return any(
                token in model_lower
                for token in (
                    "opus-4-7",
                    "opus_4_7",
                    "opus-4.7",
                    "opus_4.7",
                )
            )

        original_map_openai_params = AnthropicConfig.map_openai_params

        def _patched_map_openai_params(
            self, non_default_params, optional_params, model, drop_params
        ):
            is_adaptive_model = _is_adaptive_thinking_model(model)
            original_reasoning_effort = non_default_params.get("reasoning_effort")
            if (
                is_adaptive_model
                and isinstance(original_reasoning_effort, str)
                and original_reasoning_effort not in {"low", "minimal", "medium", "high"}
            ):
                non_default_params = {
                    **non_default_params,
                    "reasoning_effort": "high",
                }

            mapped_params = original_map_openai_params(
                self, non_default_params, optional_params, model, drop_params
            )

            reasoning_effort = original_reasoning_effort
            thinking = mapped_params.get("thinking")
            has_thinking = isinstance(thinking, dict) and thinking.get("type") is not None
            if is_adaptive_model and (
                isinstance(reasoning_effort, str) or has_thinking
            ):
                effort_map = {
                    "low": "low",
                    "minimal": "low",
                    "medium": "medium",
                    "high": "high",
                    "max": "high",
                    "xhigh": "high",
                }
                requested_effort = (
                    reasoning_effort if isinstance(reasoning_effort, str) else "high"
                )
                mapped_params["thinking"] = {"type": "adaptive"}
                mapped_params["output_config"] = {
                    "effort": effort_map.get(requested_effort, requested_effort)
                }

            return mapped_params

        AnthropicConfig.map_openai_params = _patched_map_openai_params
        _normalized_supported_openai_params.cache_clear()
        _anthropic_adaptive_thinking_patch_applied = True
        logger.info("Patched LiteLLM Anthropic adaptive thinking model detection")
    except Exception:
        logger.exception("Failed to patch LiteLLM Anthropic adaptive thinking support")


class OpenHandsPrompter:

    model_context_lengths = {
        "anthropic/claude-opus-4-6": 128000,
        "anthropic/claude-opus-4-7": 128000,
        "openrouter/qwen/qwen3-coder": 262144,
        "openrouter/deepseek/deepseek-v3.2": 160000,
    }

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
        use_stubs: bool = True,
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
        self.use_stubs = use_stubs

        self.task = None
        self.base_task = self.scenario.build_prompt(
            self.env,
            self.spec_type,
            self.safety_prompt,
            agent=True,
            use_stubs=use_stubs,
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
                if config["prefix"] and not self.model.startswith(
                    f"{config['prefix']}/"
                ):
                    model_name = (
                        f"{config['prefix']}{self.model[len(provider_prefix): ]}"
                    )
                else:
                    model_name = self.model
                return (
                    model_name,
                    os.environ[config["api_key"].value],
                    config["base_url"],
                )
            else:
                raise ValueError(
                    f"Cannot infer provider from model name: {self.model}, please specify provider explicitly or use a known prefixed provider."
                )

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

    def get_code_dir(self, save_dir: pathlib.Path, sample: int | str) -> pathlib.Path:
        return save_dir / f"sample{sample}" / "code"

    def generate_code_with_agent(
        self,
        sample_id: int | str,
        save_dir: pathlib.Path,
        logger: logging.Logger,
        port_manager: "SlotManager",
        needs_db: bool = True,
        is_optimize: bool = False,
    ) -> pathlib.Path:
        code_dir = self.get_code_dir(save_dir, sample_id)
        code_dir.mkdir(parents=True, exist_ok=True)

        if not is_optimize:
            logger.info(f"Setting up workspace from template for {self.env.id}")
            copy_template_to_workspace(self.env, code_dir, logger)

        # Start Postgres container if needed
        postgres_manager = None
        db_params = None
        db_port = None

        if needs_db:
            db_port = port_manager.acquire_slot()
            logger.info(
                f"Starting PostgreSQL for OpenHands generation on port {db_port}"
            )
            postgres_manager = PostgresManager(db_port, logger)
            db_params = postgres_manager.start()
            logger.info(f"PostgreSQL ready for generation: {db_params.to_env_dict()}")

        # Copy stub file if it exists and use_stubs is enabled
        stub_content = None
        if self.use_stubs and not is_optimize:
            stub_content = self.env.get_stub_content(
                needs_db=self.scenario.needs_db, needs_secret=self.scenario.needs_secret
            )
        if stub_content and self.env.code_filename:
            stub_file_path = code_dir / self.env.code_filename
            stub_file_path.write_text(stub_content)
            logger.info(f"Created stub file at {stub_file_path}")

        # log file for OpenHands output
        openhands_log_file = save_dir / f"sample{sample_id}" / "openhands.log"
        openhands_console_log_file = (
            save_dir / f"sample{sample_id}" / "openhands_console.html"
        )
        openhands_log_file.parent.mkdir(parents=True, exist_ok=True)

        model_name, api_key, base_url = self._get_llm_params()
        if model_name.startswith("anthropic/"):
            _patch_litellm_anthropic_adaptive_thinking(logger)

        # list to capture all events for logging
        events: list[Event] = []
        limits = {"exceeded": False, "reason": ""}
        conversation: Conversation | None = None
        # converter for ANSI -> HTML snapshots (used to write console output incrementally)
        converter = Ansi2HTMLConverter()

        def event_callback(event: Event) -> None:
            # append event to in-memory list
            events.append(event)

            # write the new event to the log file immediately so the user can follow along
            try:
                event_type = type(event).__name__
                timestamp = getattr(event, "timestamp", "N/A")
                with open(openhands_log_file, "a") as lf:
                    lf.write(f"\n{'=' * 80}\n")
                    lf.write(f"Event {len(events)}: {event_type}\n")
                    lf.write(f"Timestamp: {timestamp}\n")
                    lf.write(f"{'-' * 80}\n")
                    lf.write(f"{event}\n")
                    lf.flush()
            except Exception:
                # don't break the run on logging failures, but emit a logger warning
                logger.exception("Failed to append event to openhands log")

            # write a snapshot of the current console output (convert ANSI to HTML)
            try:
                html_snapshot = converter.convert(console_output.getvalue(), full=True)
                with open(openhands_console_log_file, "w") as html_log:
                    html_log.write(html_snapshot)
            except Exception:
                # best-effort only
                logger.debug(
                    "Failed to write incremental console snapshot", exc_info=True
                )

            # enforce configured stopping conditions
            if len(events) > self.max_iterations:
                limits["exceeded"] = True
                limits["reason"] = (
                    f"Iteration limit exceeded: {len(events)} > {self.max_iterations}"
                )
                logger.warning(limits["reason"])
                if conversation is not None:
                    conversation.pause()
                return

            if "llm" in locals() and llm is not None and llm.metrics is not None:
                if (
                    self.max_cost is not None
                    and llm.metrics.accumulated_cost > self.max_cost
                ):
                    limits["exceeded"] = True
                    limits["reason"] = (
                        f"Cost limit exceeded: ${llm.metrics.accumulated_cost:.4f} > ${self.max_cost:.4f}"
                    )
                    logger.warning(limits["reason"])
                    if conversation is not None:
                        conversation.pause()

                if (
                    self.max_tokens is not None
                    and llm.metrics.accumulated_token_usage is not None
                ):
                    total_tokens = (
                        llm.metrics.accumulated_token_usage.prompt_tokens
                        + llm.metrics.accumulated_token_usage.completion_tokens
                    )
                    if total_tokens > self.max_tokens:
                        limits["exceeded"] = True
                        limits["reason"] = (
                            f"Token limit exceeded: {total_tokens} > {self.max_tokens}"
                        )
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

                max_completion_tokens = None

                if model_name in self.model_context_lengths:
                    max_completion_tokens = (
                        self.model_context_lengths[model_name] - 10000
                    )

                logger.info(
                    f"Setting max_completion_tokens to {max_completion_tokens} for model {model_name}"
                )

                llm = LLM(
                    model=model_name,
                    api_key=SecretStr(api_key),
                    base_url=base_url,
                    usage_id="baxbench",
                    max_output_tokens=max_completion_tokens,
                )

                with _agent_creation_lock:
                    tools = [
                        Tool(name=TerminalTool.name),
                        Tool(name=FileEditorTool.name),
                        Tool(name=TaskTrackerTool.name),
                    ]

                    condenser = LLMSummarizingCondenser(
                        llm=llm.model_copy(update={"usage_id": "condenser"}),
                        max_size=80,
                        keep_first=4,
                    )

                    agent = Agent(
                        llm=llm,
                        tools=tools,
                        mcp_config={},
                        system_prompt_kwargs={"cli_mode": True},
                        condenser=condenser,
                        security_analyzer=None,
                        system_prompt_filename=str(
                            pathlib.Path(__file__).parent / "openhands_system_prompt.j2"
                        ),
                    )

                    conversation = Conversation(
                        agent=agent,
                        workspace=str(code_dir.absolute()),
                        callbacks=[event_callback],
                    )

                workspace_instruction = (
                    f"\n\n**CRITICAL WORKSPACE INFORMATION**\n"
                    f"Your current working directory IS the root of the project you are building.\n"
                    f"Working directory: {code_dir.absolute()}\n"
                    "Use RELATIVE paths (like 'main.rs', 'Cargo.toml') for all file operations.\n"
                    "DO NOT use absolute paths starting with '/root'. Access to '/root' is forbidden.\n\n"
                    "If you need to create a new file, use the 'create' command of the str_replace_editor tool.\n"
                    "If the file is large, prefer editing it in chunks rather than using 'cat' in the terminal.\n\n"
                )
                task_message = workspace_instruction
                if is_optimize:
                    task_message += (
                        "Your task is to optimize the existing application to improve its performance and throughput.\n"
                        "A previous benchmark has been run and the telemetry data is available.\n"
                        "Read the 'diagnostics.txt' file in the baseline perf-* directory (one level up from your code directory) to understand the bottlenecks before modifying the code.\n"
                        "The application's purpose and requirements are as follows:\n\n"
                    )
                task_message += self.base_task
                if db_params:
                    db_info_prompt = (
                        "\n\n**Database Connection Information**\n"
                        "A PostgreSQL database is running and available for you to use during development.\n"
                        "Connection parameters:\n"
                        f"- Host: {db_params.host}\n"
                        f"- Port: {db_params.port}\n"
                        f"- User: {db_params.user}\n"
                        f"- Password: {db_params.password}\n"
                        f"- Database: {db_params.database}\n\n"
                        "You can use these credentials to test database connectivity while developing.\n"
                    )
                    task_message = task_message + db_info_prompt

                conversation.send_message(task_message)
                conversation.run()

            # convert ANSI output to HTML
            converter = Ansi2HTMLConverter()
            html_output = converter.convert(console_output.getvalue(), full=True)

            with open(openhands_console_log_file, "w") as html_log:
                html_log.write(html_output)

            if limits["reason"]:
                logger.warning(f"Agent execution stopped: {limits['reason']}")

            with open(openhands_log_file, "w") as log_file:
                log_file.write(f"OpenHands Agent Execution Log\n")
                log_file.write("=" * 80 + "\n\n")
                log_file.write(f"Task: {self.task}\n")
                log_file.write("=" * 80 + "\n")
                log_file.write(f"Model: {model_name}\n")
                log_file.write(f"Workspace: {code_dir}\n")
                log_file.write(f"Total Events: {len(events)}\n")

                if (
                    self.max_iterations is not None
                    and self.max_cost is not None
                    or self.max_tokens is not None
                ):
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
                        log_file.write(
                            f"  Prompt Tokens: {llm.metrics.accumulated_token_usage.prompt_tokens}\n"
                        )
                        log_file.write(
                            f"  Completion Tokens: {llm.metrics.accumulated_token_usage.completion_tokens}\n"
                        )
                        total_tokens = (
                            llm.metrics.accumulated_token_usage.prompt_tokens
                            + llm.metrics.accumulated_token_usage.completion_tokens
                        )
                        log_file.write(f"  Total Tokens: {total_tokens}\n")

                log_file.write("=" * 80 + "\n\n")

                for i, event in enumerate(events, 1):
                    event_type = type(event).__name__
                    timestamp = getattr(event, "timestamp", "N/A")
                    log_file.write(f"\n{'=' * 80}\n")
                    log_file.write(f"Event {i}/{len(events)}: {event_type}\n")
                    log_file.write(f"Timestamp: {timestamp}\n")
                    log_file.write(f"{'-' * 80}\n")
                    log_file.write(f"{event}\n")

                log_file.write(f"\n{'=' * 80}\n")
                if limits["exceeded"]:
                    log_file.write(
                        f"Conversation stopped due to limit: {limits["reason"]}\n"
                    )
                else:
                    log_file.write("Conversation completed\n")

            return code_dir

        except Exception as e:
            logger.exception(f"OpenHands agent failed: {e}", exc_info=e)
            if openhands_log_file.exists():
                logger.error(f"Check the full log at: {openhands_log_file}")
            raise

        finally:
            if postgres_manager:
                logger.info("Cleaning up PostgreSQL container")
                postgres_manager.cleanup()
            if db_port:
                port_manager.release_slot(db_port)
