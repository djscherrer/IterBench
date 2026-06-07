# Parameters used for this generation

Commit `24f943773987e73fb8b5bfaf20d22eb4d21800aa` Mon Apr 6, postgres-integration branch

```
pipenv run python src/main.py --models deepseek/deepseek-v3.2 anthropic/claude-opus-4.6 openai/gpt-5.4 --provider openrouter --mode generate --n_samples 3
```

With the defaults this gives us:

```
Namespace(models=['deepseek/deepseek-v3.2', 'anthropic/claude-opus-4.6', 'openai/gpt-5.4'], mode='generate', temperature=0.2, n_samples=3, reasoning_effort='high', only_samples=None, ks=None, envs=None, exclude_envs=None, scenarios=None, exclude_scenarios=None, spec_type='openapi', safety_prompt='none', results_dir=PosixPath('/home/max/eth/baxbench/results'), max_concurrent_runs=None, timeout=300, num_ports=10000, min_port=12345, max_retries=2, base_delay=1.0, max_delay=128.0, bench_app_host=None, bench_app_private_addr=None, bench_loader_host=None, bench_remote_dir='/tmp/baxbench', bench_remote_port=None, force=False, skip_failed=False, prune_docker=False, vllm_port=8000, use_openhands=False, use_claude_agent=False, agent_cls='CodeActAgent', agent_max_iterations=50, agent_max_cost=None, agent_max_tokens=None, port=5001, provider='openrouter', use_stubs=False, run_security_tests=False)
```