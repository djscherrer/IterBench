from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    # Same .env baxbench's own main.py loads (repo root, matching .env.example) —
    # API keys are shared, not a separate scenario_builder-local file.
    load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env", override=False)

from config import args
from exploit_gen.cwe_lut import fetch_cwes
from export.latest import export_latest_snapshot
from exploit_gen.pipeline import generate_exploits
from functional.pipeline import generate_and_iterate_tests
from performance.generate import generate_performance
from scenario_gen.generate import generate_scenarios

if __name__ == "__main__":
    if args.generate_scenarios:
        generate_scenarios()
    elif args.generate_tests:
        generate_and_iterate_tests()
        # verify_tests()
    elif args.generate_performance:
        generate_performance()
    elif getattr(args, "export_latest", False):
        export_latest_snapshot()
    else:
        fetch_cwes()
        generate_exploits()
