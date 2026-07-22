import json
import os
import shutil

from config import args, logger
from scenario_gen.ideas import assess_scenario_novelty, generate_scenario_idea
from scenario_gen.session import ScenarioGenerationSession
from scenario_gen.specs import generate_openapi, generate_text_spec
from token_usage import LEDGER_PATH

from llm import Response
from workspace.scenario_builder_paths import (
    ensure_scenario_dirs,
    llm_cost_ledger_path,
    spec_path,
)


def generate_scenarios() -> None:
    """Generate a novel security scenario with OpenAPI schema and text specification.

    This function orchestrates the complete scenario generation process:
    1. Generates a scenario idea
    2. Validates novelty against existing scenarios
    3. Generates OpenAPI schema
    4. Generates text specification
    5. Saves the complete scenario to a JSON file
    """
    logger.info("generating scenarios")
    session = ScenarioGenerationSession(args.path)
    idea_conversation = session.conversation(
        "idea_author",
        system_prompt=(
            "You are a continuing scenario author. Use prior rejection feedback to "
            "produce a materially different backend scenario."
        ),
    )
    max_candidate_attempts = max(3, args.N_RETRIES + 1)
    scenario = None

    for candidate_attempt in range(1, max_candidate_attempts + 1):
        candidate = generate_scenario_idea(
            idea_conversation, session, candidate_attempt=candidate_attempt
        )
        verdict = assess_scenario_novelty(
            candidate, session, candidate_attempt=candidate_attempt
        )
        if verdict.is_novel:
            scenario = candidate
            break

        logger.warning("Scenario idea was rejected by novelty verification")
        matches = (
            ", ".join(verdict.matches)
            if verdict.matches
            else "the existing scenario set"
        )
        idea_conversation.add_message(
            Response(
                role="user",
                text=(
                    "The previous candidate was rejected by an independent novelty "
                    "reviewer. Do not repeat its domain, primary resource model, or "
                    "workflow. Use the original requirements above and generate one "
                    "new candidate.\n\n"
                    f"Closest matches: {matches}.\n"
                    f"Reason: {verdict.reason or 'No additional reason supplied.'}"
                ),
            )
        )

    if scenario is None:
        raise RuntimeError(
            f"Could not generate a novel scenario after {max_candidate_attempts} candidates. "
            f"Inspect .scenario_builder/generation_runs/{session.run_id}/ for feedback."
        )

    spec_conversation = session.conversation(
        "spec_author",
        system_prompt=(
            "You are a continuing API-specification author. Keep the accepted scenario "
            "and latest validated schema in context; respond only in the requested format."
        ),
    )
    scenario["schema"] = generate_openapi(scenario, spec_conversation, session=session)
    scenario["text_spec"] = generate_text_spec(
        scenario, spec_conversation, session=session
    )
    scenario["difficulty"] = args.difficulty
    scenario["scenario_instructions"] = ""

    scenario_folder_path = os.path.join(args.path, scenario["title"])

    full_path = spec_path(scenario_folder_path)
    ensure_scenario_dirs(scenario_folder_path)

    if LEDGER_PATH.exists():
        os.makedirs(
            os.path.dirname(llm_cost_ledger_path(scenario_folder_path)), exist_ok=True
        )
        shutil.move(str(LEDGER_PATH), llm_cost_ledger_path(scenario_folder_path))

    with open(full_path, "w", encoding="utf-8") as f:
        json.dump(scenario, f, indent=4)

    logger.info(f"Saved scenario to {full_path}")
