import glob
import logging
import os
import pathlib
from argparse import ArgumentParser

# Must run before anything below imports env/scenarios/cwes/llm/tasks: those
# now resolve to baxbench's own copies via this sys.path append, since
# AutoBaxScale no longer keeps local forks of them.
import _bootstrap  # noqa: F401  (baxbench src/ onto sys.path)

from workspace.scenario_builder_paths import ensure_scenario_dirs, spec_path
from token_usage import get_model

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


def _provider_for_model(model: str) -> str:
    if "claude" in model:
        return "anthropic"
    if model.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    # Third-party/vendor-prefixed catalog names (e.g. "z-ai/glm-5.2",
    # "deepseek/deepseek-v3", "meta-llama/llama-3.3-70b-instruct") are only
    # reachable through OpenRouter in this codebase.
    return "openrouter"


def build_tasks(scenario, model_list=None, env_list=None) -> list:
    """Build baxbench Task objects for a scenario — one per (env, model).

    Replaces the old get_baxbench_args()+baxbench_wrapper.main() round-trip
    through an argparse Namespace: callers now build real Task/TaskHandler
    objects directly and drive them with plain Python calls.
    """
    from env import all_envs
    from tasks import Task

    # `args` (parsed below, module-level) carries --models/--envs from the CLI
    # (see scripts/build_scenarios.sh) unless overridden by the params here.
    models = model_list or args.models
    env_ids = env_list or args.envs
    if not models:
        raise ValueError(
            "No models specified: pass --models (see MODELS in scripts/build_scenarios.sh) "
            "or model_list=..."
        )
    if not env_ids:
        raise ValueError(
            "No envs specified: pass --envs (see ENVS in scripts/build_scenarios.sh) "
            "or env_list=..."
        )
    envs = [e for e in all_envs if e.id in env_ids]
    return [
        Task(
            env=env,
            scenario=scenario,
            model=model,
            temperature=0.0,
            reasoning_effort="high",
            spec_type="openapi",
            safety_prompt="none",
            provider=_provider_for_model(model),
        )
        for env in envs
        for model in models
    ]


# gen_scenarios/ sits at the repo root, alongside src/ and baxbench's own
# results/ — a scenario_builder-specific counterpart, not buried inside src/.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
GEN_SCENARIOS_DIR = _REPO_ROOT / "gen_scenarios"

# Results directory for build_tasks()-driven Task/TaskHandler runs (matches
# the old baxbench_wrapper.py default: a "results" dir next to this package).
RESULTS_DIR = GEN_SCENARIOS_DIR / "results"


parser = ArgumentParser()
parser.add_argument(
    "--difficulty",
    type=int,
    default=5,
    help="Difficulty of the backend, characterized as max. number of endpoints",
)
parser.add_argument(
    "--N_RETRIES",
    type=int,
    default=3,
    help="Max. number of attempts to fix invalid/erroronous/unparsable output in an agentic loop",
)
parser.add_argument(
    "--N_SOL_STEPS",
    type=int,
    default=5,
    help="Number of solution iteration steps",
)
parser.add_argument(
    "--N_TEST_STEPS",
    type=int,
    default=5,
    help="Number of test iteration steps",
)

parser.add_argument(
    "--N_SEC_STEPS",
    type=int,
    default=5,
    help="Number of security test iterations",
)

parser.add_argument(
    "--debug",
    action="store_true",
    help="Debug mode",
)
parser.add_argument(
    "--path",
    default=str(GEN_SCENARIOS_DIR / "artifacts"),
    help="Path to artifacts folder",
)
parser.add_argument(
    "--models",
    type=str,
    nargs="+",
    default=None,
    help="Models to use for solution generation (see MODELS in scripts/build_scenarios.sh)",
)
parser.add_argument(
    "--envs",
    type=str,
    nargs="+",
    default=None,
    help="Envs to generate/test solutions in, e.g. Python-Flask (see ENVS in scripts/build_scenarios.sh)",
)
parser.add_argument(
    "--reasoning_model",
    type=str,
    default=None,
    help=(
        "Model powering this package's own agent/reasoning steps (idea, spec, "
        "exploit, and functional-test generation + iteration) — provider is "
        "inferred the same way as --models. See REASONING_MODEL in "
        "scripts/build_scenarios.sh. Required for every mode except --export_latest."
    ),
)

group = parser.add_mutually_exclusive_group(required=True)
group.add_argument("--generate_scenarios", action="store_true", help="Generate scenarios")
group.add_argument("--generate_tests", action="store_true", help="Generate tests")
group.add_argument("--generate_exploits", action="store_true", help="Generate exploits")
group.add_argument("--generate_performance", action="store_true", help="Generate performance tests")
group.add_argument(
    "--export_latest",
    action="store_true",
    help="Export latest scenario snapshot into scenarios directory",
)

parser.add_argument(
    "--scenario",
    type=str,
    help="Scenario name (required if --generate_tests or --generate_exploits is set)",
)

parser.add_argument(
    "--export_dir",
    type=str,
    default=os.path.join("..", "..", "src", "scenarios", "generated_scenarios"),
    help="Directory to export latest snapshot into (used with --export_latest)",
)

args = parser.parse_args()

if (args.generate_tests or args.generate_exploits) and not args.scenario:
    parser.error(
        "--scenario is required when using --generate_tests or --generate_exploits"
    )

if not getattr(args, "export_latest", False) and not args.reasoning_model:
    parser.error("--reasoning_model is required for every mode except --export_latest")

reasoning_model = (
    get_model(args.reasoning_model, _provider_for_model(args.reasoning_model), True, "medium")
    if args.reasoning_model
    else None
)

logger.info(f"Parsed command-line arguments: {args}")

# Verify that the provided arguments are valid
if not os.path.exists(args.path):
    parser.error(f"Invalid path {args.path}")

scenario_folder_path = os.path.join(
    args.path, args.scenario if args.scenario is not None else ""
)
if not os.path.exists(scenario_folder_path):
    parser.error(f"Invalid path {scenario_folder_path}")

if args.scenario and not os.path.isfile(spec_path(scenario_folder_path)):
    parser.error(
        f"File {spec_path(scenario_folder_path)} not found for scenario {args.scenario}"
    )

if args.scenario:
    ensure_scenario_dirs(scenario_folder_path)

for file in glob.glob(os.path.join(args.path, "llm_cost_ledger*")):
    os.remove(file)
