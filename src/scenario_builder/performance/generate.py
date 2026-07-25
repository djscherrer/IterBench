import json
import os
import re
from typing import Optional

import templates
from config import args, logger, reasoning_model, scenario_folder_path
from utils import AgentException, agentic_loop

from llm import Conversation, Response
from workspace.scenario_builder_paths import (
    implementation_path,
    latest_index,
    snapshot_path,
    spec_path,
)


def parse_locust_code(conversation: Conversation) -> str:
    """Parses Locust script code from the conversation."""
    raw_response = conversation.responses[-1].text
    match = re.search(
        r"<LOCUST_SCRIPT>\s*```(?:python\s*)?(.*?)```\s*</LOCUST_SCRIPT>",
        raw_response,
        re.DOTALL,
    )

    if not match:
        raise AgentException(
            "ParseError",
            "Could not parse <LOCUST_SCRIPT> section.",
        )

    locust_code = match.group(1).strip()

    # Check if code is compilable
    try:
        compile(locust_code, "<string>", "exec")
    except SyntaxError as e:
        raise AgentException(
            "SyntaxError",
            f"Unable to compile Locst Script natively. SyntaxError: {e}",
        )

    if not uses_fast_http_user(locust_code):
        raise AgentException(
            "ConsistencyError",
            "The generated script must define a class that directly extends FastHttpUser.",
        )

    return locust_code


def uses_fast_http_user(locust_code: str) -> bool:
    """Whether a generated script directly uses Locust's fast user class."""
    return bool(
        re.search(r"from\s+locust\s+import[^\n]*\bFastHttpUser\b", locust_code)
        and re.search(r"class\s+\w+\s*\(\s*FastHttpUser\s*\)", locust_code)
    )


def _prompt_with_rollback(
    conversation: Conversation, prompt: str, *, purpose: str, on_response=None
) -> Response:
    """Adds a user turn, persists it, and calls the model -- rolling the
    turn back out (and re-persisting) if the call raises, instead of
    leaving a dangling user message with no reply on disk. Left in place,
    that dangling turn would make the *next* attempt's ``conversation.responses``
    non-empty and wrongly treated as a continuation requiring feedback, even
    though this attempt never actually got a response.
    """
    conversation.add_message(Response(role="user", text=prompt))
    if on_response is not None:
        on_response()
    try:
        response = reasoning_model.generate(
            conversation,
            temperature=0.2,
            purpose=purpose,
        )
    except Exception:
        conversation.remove_message()
        if on_response is not None:
            on_response()
        raise
    conversation.add_message(response)
    if on_response is not None:
        on_response()
    return response


def generate_locust_code(
    scenario: dict,
    conversation: Conversation,
    feedback: Optional[str] = None,
    *,
    on_response=None,
    on_failure=None,
) -> str:
    """Generates Python code for the Locust performance script."""

    if conversation.responses:
        if not feedback:
            raise ValueError(
                "A continuing Locust conversation requires failure feedback."
            )
        prompt = (
            f"{feedback}\n\nReturn one corrected complete Locust script in the required "
            "format. The scenario contract and previous complete script are already in "
            "this conversation; do not repeat them in prose."
        )
    else:
        prompt = templates.generate_locust_script.format(
            scenario_title=scenario["title"],
            scenario_description=scenario["description"],
            scenario_openapi=scenario["schema"],
            scenario_performance_objectives=json.dumps(
                scenario.get("performance_objectives", {}), indent=2
            ),
            locust_code_template=templates.locust_code_template,
        )

    _prompt_with_rollback(
        conversation,
        prompt,
        purpose="generate_locust_script: generating locust code",
        on_response=on_response,
    )

    logger.info("Generated Locust script code")

    # parse, verify consistency and check if compiles
    locust_code = agentic_loop(
        conversation,
        parse_locust_code,
        args.N_RETRIES,
        "parsing and verifying compilability of the locust script code",
        templates.locust_code_template,
        on_response=on_response,
        on_failure=on_failure,
    )

    return locust_code


def parse_review_decision(conversation: Conversation) -> Optional[str]:
    """Parses the load-review turn's response.

    Returns ``None`` if the author explicitly decided nothing needs to
    change (``<DECISION>NOTHING_TO_FIX</DECISION>``), or the validated
    revised script text otherwise. An unrecognized ``<DECISION>`` value is a
    parse error (retried like any other); a response with no ``<DECISION>``
    tag at all is assumed to be a script and parsed as one.
    """
    raw_response = conversation.responses[-1].text
    decision_match = re.search(
        r"<DECISION>\s*(.*?)\s*</DECISION>", raw_response, re.DOTALL
    )
    if decision_match:
        decision = decision_match.group(1).strip().upper()
        if decision == "NOTHING_TO_FIX":
            return None
        raise AgentException(
            "ParseError", f"Unrecognized <DECISION> value: {decision!r}"
        )
    return parse_locust_code(conversation)


def review_locust_code(
    scenario: dict,
    conversation: Conversation,
    load_results_report: str,
    *,
    on_response=None,
    on_failure=None,
) -> Optional[str]:
    """Sends stage-2 cross-implementation load results to the author and
    asks it to either fix a genuine script-side problem or explicitly say
    there's nothing to fix. Returns ``None`` in the latter case, or the
    validated revised script otherwise.
    """
    decision_format = templates.review_locust_decision_format.format(
        locust_code_template=templates.locust_code_template
    )
    prompt = templates.review_locust_load_results.format(
        load_results_report=load_results_report,
        review_decision_format=decision_format,
    )
    _prompt_with_rollback(
        conversation,
        prompt,
        purpose="review_locust_script: reviewing cross-implementation load results",
        on_response=on_response,
    )

    logger.info("Reviewed Locust script against cross-implementation load results")

    return agentic_loop(
        conversation,
        parse_review_decision,
        args.N_RETRIES,
        "parsing the load-review decision (nothing-to-fix or a corrected locust script)",
        decision_format,
        on_response=on_response,
        on_failure=on_failure,
    )


def _pick_performance_input_file(stem: str) -> str:
    """
    Choose the best scenario snapshot to base performance generation on.
    Preference order:
      1) latest performance snapshot: snapshots/performance/ipN.json
      2) latest test snapshot: snapshots/functional/iuN.json
      3) base scenario spec: spec/scenario.json
    """
    ip_latest = latest_index(scenario_folder_path, "ip")
    if ip_latest is not None:
        return snapshot_path(scenario_folder_path, "ip", ip_latest)

    iu_latest = latest_index(scenario_folder_path, "iu")
    if iu_latest is not None:
        return snapshot_path(scenario_folder_path, "iu", iu_latest)

    return spec_path(scenario_folder_path)


def _next_performance_output_file(stem: str) -> str:
    ip_latest = latest_index(scenario_folder_path, "ip")
    next_idx = 0 if ip_latest is None else ip_latest + 1
    return snapshot_path(scenario_folder_path, "ip", next_idx)


def generate_performance() -> None:
    """Entry point for generating Locust performance scripts."""
    # Local import: generate_and_verify_locust_script (performance/locust_pipeline.py)
    # itself imports generate_locust_code from this module, so importing it at
    # module level here would be circular.
    from performance.locust_pipeline import (
        ensure_locust_conversation,
        generate_and_verify_locust_script,
    )

    logger.info("Generating performance tests")

    # Load scenario snapshot without mutating iu* files.
    scenario_file = _pick_performance_input_file(args.scenario)
    with open(scenario_file, "r", encoding="utf-8") as file:
        scenario = json.load(file)
    ensure_locust_conversation(scenario)

    # Load the best known implementations
    implementations = {}
    impl_file = ""
    for it in range(args.N_SOL_STEPS + 1, -1, -1):
        test_file = implementation_path(scenario_folder_path, "it", it)
        if os.path.exists(test_file):
            impl_file = test_file
            break

    if impl_file:
        with open(impl_file, "r") as file:
            raw_implementations = json.load(file)
            implementations = raw_implementations
    else:
        logger.warning(
            "Could not find any implementations to test against. Locust script verification will be skipped."
        )

    # Filter out empty implementation dicts (these cannot boot a server and will fail with missing app.py)
    non_empty_implementations = {
        k: v for k, v in implementations.items() if isinstance(v, dict) and len(v) > 0
    }
    if implementations and not non_empty_implementations:
        logger.warning(
            "Found implementations file %s, but all implementations are empty. "
            "Skipping Locust verification.",
            impl_file or "<unknown>",
        )

    # Resolve environment
    environment = None

    if not environment and non_empty_implementations:
        from env import all_envs

        # Pick a reference implementation that actually has files.
        ref_key = next(iter(non_empty_implementations.keys()))
        logger.info(
            "Using reference implementation for Locust verification: %s", ref_key
        )
        key_parts = ref_key.split()
        env_lang, env_framework = key_parts[0], key_parts[1]

        for e_inst in all_envs:
            if (
                e_inst.language.lower() == env_lang.lower()
                and e_inst.framework.lower() == env_framework.lower()
            ):
                environment = e_inst
                break

    if environment:
        # Allocate an ip index for this performance run and let the locust pipeline
        # write the locustfile snapshot for the attempt.
        ip_latest = latest_index(scenario_folder_path, "ip")
        next_ip_idx = 0 if ip_latest is None else ip_latest + 1
        ip_out_dir = os.path.join(scenario_folder_path, "snapshots", "performance")
        os.makedirs(ip_out_dir, exist_ok=True)
        scenario, final_ip_idx = generate_and_verify_locust_script(
            scenario,
            environment,
            non_empty_implementations,
            ip_start=next_ip_idx,
            ip_out_dir=ip_out_dir,
            stem="ip",
        )

        # Always persist the scenario snapshot for the final attempt to ip*.json
        save_idx = final_ip_idx if final_ip_idx is not None else next_ip_idx
        save_path = snapshot_path(scenario_folder_path, "ip", save_idx)
        with open(save_path, "w", encoding="utf-8") as file:
            json.dump(scenario, file, indent=4)

        logger.info(f"Performance tests linked and saved to {save_path}")
        return

    # If we couldn't resolve an environment (or have no impls), still save a snapshot
    # so downstream tooling can pick it up.
    save_path = _next_performance_output_file(args.scenario)
    with open(save_path, "w", encoding="utf-8") as file:
        json.dump(scenario, file, indent=4)
    logger.info(f"Performance tests linked and saved to {save_path}")
