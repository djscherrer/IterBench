# gen_scenarios/

Local, gitignored working directory for `src/scenario_builder/` (AutoBaxBuilder,
[paper](https://arxiv.org/abs/2512.21132)) — the LLM-driven pipeline that
bootstraps new BaxBench-style scenarios. Nothing here is tracked by git and
nothing here is read by the main `k8s_bench` evaluation; it's scratch state
from running the scenario builder, safe to delete and regenerate. See the
"Generating new scenarios" section of the top-level `README.md` for how to
drive the pipeline; this file just documents what it leaves behind.

## `artifacts/<ScenarioName>/`

One folder per scenario the builder has worked on, holding the full pipeline
state for that scenario (layout defined in
`src/workspace/scenario_builder_paths.py`):

- `spec/scenario.json`, `tasklist.json` — the canonical scenario definition
- `snapshots/{functional,security,performance}/{iu,iw,ip}{n}.json` — state
  after each iteration step
- `implementations/{it,iu,iw}{n}.json` — generated reference solutions
- `results/{tag}{n}/summary.json` + logs — test/exploit/perf run results
- `exports/{tag}{n}.py` — the BaxBench-ready module for a given snapshot
- `conversations/` — the LLM chat threads behind each step
- `logs/llm_cost_ledger.json`, `logs/verdicts.txt`
- `failures/{functional,performance}/` — failed attempts, kept for debugging

The scenarios currently sitting here (`SortCascade_ControlledDataTransformation`,
`GearLoopEquipmentCheckout`, `LockerDropParcelExchange`,
`PollDynamix_SecureVoteAggregator`, `SplitLedgerGroupExpenseBalancer`,
`TimeCapsuleMessageLocker`, `CodeSnippetVault_SecureSandbox`,
`DocumentArchive_MultiTenantFileSystem`) are all **unpromoted, in-progress
work** — none of them has been reviewed and moved into `src/scenarios/` yet,
despite some topical overlap with scenarios that already have (e.g. a parcel
locker, a time-capsule note vault, a vote aggregator, an expense splitter
already exist under different names as promoted scenarios).

### `artifacts/.scenario_builder/generation_runs/<run-id>/`

Idea/spec-authoring conversations and failure records from before a
candidate scenario earns a name — i.e. state from the `generate_scenarios`
step, prior to becoming one of the named folders above.

## `results/<model>/<Scenario>/<Env>/temp0.0-openapi-none/`

BaxBench-style test-run output from testing **reference-solution models**
during scenario authoring (the builder's `generate_tests` /
`generate_performance` steps) — this is a completely different set of models
from the thesis's 3-model main benchmark (`claude-opus-4-8`, `gpt-5.5`,
`glm-5.2`, see `scripts/bench_k8s.sh`). None of the models here
(`moonshotai-kimi-k3`, `google-gemini-3.6-flash`, `deepseek-deepseek-v4-flash`,
`deepseek-deepseek-v3.2`) appear anywhere in the main benchmark; they exist
only to validate that a newly authored scenario's tests/exploits are sound.

`z.ai-glm-5.2/` (dot) vs `z-ai-glm-5.2/` (dash): two literal different
`--models` strings typed on different runs, not a bug in either the builder
or this doc. The dot-form is a stale, single-scenario leftover from before
the model id string was settled (Jul 23 00:57); the dash-form is the
complete run across all 7 scenarios (Jul 23 14:38) and matches the id used
in `scripts/build_scenarios.sh`.

## `build_tests_retry.log`

Raw stdout from one `--generate_tests` invocation on a remote scratch copy
of the repo, ending mid-run in a shell syntax error. A leftover debugging
log, not meaningful artifact data — safe to delete.
