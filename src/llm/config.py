"""Per-model limits and provider routing tables for :class:`Prompter`."""

from __future__ import annotations

SYSTEM_PROMPT = "You are an experienced full-stack developer"

NATIVE_PROVIDER_PREFIXES = (
    "anthropic",
    "openrouter",
    "together_ai",
    "swissai",
    "openai",
)

# NOTE: unused because Together expects you to set
# max_tokens=context_length-numTokens(prompt)
# so we hardcode below for now
OPENAI_TOGETHER_CONTEXT_LENGTHS: dict[str, int] = {
    "mistralai/Mixtral-8x22B-Instruct-v0.1": 65536,
    "meta-llama/Llama-3.3-70B-Instruct-Turbo": 131072,
    "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free": 8100,
    "deepseek-ai/DeepSeek-V3": 131072,
    "Qwen/Qwen2.5-Coder-32B-Instruct": 32768,
    "Qwen/Qwen2.5-72B-Instruct-Turbo": 32768,
    "Qwen/Qwen2.5-7B-Instruct-Turbo": 32768,
    "Qwen/Qwen3-Next-80B-A3B-Thinking": 32768,
    "gpt-4o": 128000,
    "chatgpt-4o-latest": 128000,
    "gpt-4.1-2025-04-14": 32000,
    "gpt-4.1-mini-2025-04-14": 32000,
    "o1": 200000,
    "o1-mini": 128000,
    "o3-mini": 200000,
    "deepseek-ai/DeepSeek-R1": 164000,
    "google/gemma-2-27b-it": 8192,
    "deepseek-ai/DeepSeek-R1-Distill-Llama-70B": 131072,
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B": 131072,
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B": 131072,
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B": 131072,
    "deepseek/deepseek-v4-pro": 1_000_000,
    "deepseek/deepseek-v4-flash": 1_000_000,
    "z-ai/glm-5.2": 1_000_000,
    "moonshotai/kimi-k3": 1_000_000,
    "Qwen/QwQ-32B": 32768,
    "qwen/qwq-32b": 128000,
    "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8": 524288,
    "meta-llama/Llama-4-Scout-17B-16E-Instruct": 327680,
    "google/gemini-2.5-pro-preview-03-25": 65000,
    "google/gemini-3.6-flash": 1_000_000,
    "mistralai/mistral-small-3.1-24b-instruct": 33000,
    "google/gemma-3-27b-it": 32000,
    "meta-llama/llama-4-scout": 32000,
    "deepseek/deepseek-chat-v3-0324": 16000,
    "mistral/ministral-8b": 128000,
    "x-ai/grok-3-beta": 128000,
    "x-ai/grok-3-mini-beta": 128000,
    "Qwen/Qwen3-235B-A22B-fp8-tput": 40000,
    "qwen/qwen3-235b-a22b": 40000,
    "deepseek/deepseek-r1-0528": 32000,
    "x-ai/grok-4": 256000,
    "qwen/qwen3-coder": 200000,
    "openai/gpt-5": 256000,
    "deepseek/deepseek-v3.2": 160000,
    "gpt-5.4-mini": 400000,
    "openai/gpt-5.4-mini": 400000,
    "gpt-5.4-nano": 128000,
    # GPT-5.5 / Claude Opus OpenRouter ids (must match --models exactly)
    "gpt-5.5-2026-04-23": 1_050_000,
    "openai/gpt-5.5-2026-04-23": 1_050_000,
    "anthropic/claude-opus-4-8": 1_000_000,
    "anthropic/claude-opus-4-7": 1_000_000,
    "anthropic/claude-opus-4-6": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
}

ANTHROPIC_THINKING_LENGTHS: dict[str, int] = {
    "claude-opus-4-20250514": 32000,
    "claude-sonnet-4-20250514": 64000,
    "claude-3-7-sonnet-20250219": 64000,
    "claude-opus-4-1-20250805": 32000,
    "claude-opus-4-8": 128000,
    "claude-opus-4-7": 128000,
    "claude-opus-4-6": 128000,
}

VLLM_CONTEXT_LENGTHS: dict[str, int] = {
    "sri-blaze/kodcode -v1": 32000,
    "eth-sri/kodcode-v1-qwq-3-48-1e-5": 32000,
    "agentica-org/DeepCoder-14B-Preview": 32000,
    "eth-sri/kodcode-v1-multi-clear-qwq-3-48-1e-5": 32000,
    "eth-sri/qwq-cybernative3k_kodcodeV1": 32000,
    "eth-sri/qwq-cybernative3k_snyk_kodcodeV1": 32000,
    "eth-sri/deepseek-r1-distill-qwen-7b-cybernative-snyk-kodcodeV1": 32000,
    "openai/gpt-oss-120b": 16384,
    "openai/gpt-oss-20b": 16384,
}

OPENAI_MAX_COMPLETION_TOKENS: dict[str, int] = {
    "gpt-4o": 16384,
    "chatgpt-4o-latest": 16384,
    "o1": 100000,
    "o1-mini": 65536,
    "o3-mini": 100000,
    "gpt-4.1-2025-04-14": 32000,
    "gpt-4.1-mini-2025-04-14": 32000,
    "o3-2025-04-16": 100000,
    "o4-mini-2025-04-16": 100000,
    "gpt-5-2025-08-07": 128000,
    "gpt-5.4": 128000,
    "gpt-5.4-2026-03-05": 128000,
    "gpt-5.5-2026-04-23": 128000,
    "openai/gpt-5.5-2026-04-23": 128000,
    "gpt-5.4-mini": 128000,
    "gpt-5.4-nano": 128000,
}

OPENROUTER_REMAP: dict[str, str] = {
    "meta-llama/Llama-3.3-70B-Instruct-Turbo": "meta-llama/llama-3.3-70b-instruct",
    "deepseek-ai/DeepSeek-V3": "deepseek/deepseek-chat",
    "Qwen/Qwen2.5-Coder-32B-Instruct": "qwen/qwen-2.5-coder-32b-instruct",
    "Qwen/Qwen2.5-7B-Instruct-Turbo": "qwen/qwen-2.5-7b-instruct",
    "Qwen/Qwen2.5-72B-Instruct-Turbo": "qwen/qwen-2.5-72b-instruct",
    "Qwen/Qwen3-235B-A22B-fp8-tput": "qwen/qwen3-235b-a22b",
}
