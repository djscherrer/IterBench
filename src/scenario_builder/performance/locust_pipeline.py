"""Persistent author/verify loop for generated Locust scripts."""

from __future__ import annotations

import hashlib
from pathlib import Path

from config import args, logger, scenario_folder_path
from export.render import export_locust_code
from performance.failure import (
    LocustFailureRecord,
    locust_script_digest,
    persist_locust_failure,
)
from performance.generate import (
    generate_locust_code,
    uses_deterministic_smoke_contract,
    uses_fast_http_user,
)
from performance.verify import (
    inspect_endpoint_coverage,
    inspect_request_health,
    run_locust,
)

from env.base import Env
from llm import Conversation
from scenario_builder.conversation_store import load_conversation, persist_conversation
from scenarios.base import Scenario
from workspace.scenario_builder_paths import (
    locust_candidate_path,
    locust_conversation_path,
)


def _locust_cache_key(scenario: dict) -> str:
    source = f"{scenario.get('title', '')}\x1f{scenario.get('schema', '')}"
    return (
        "scenario-builder:locust:"
        + hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
    )


def _load_or_create_conversation(scenario: dict) -> Conversation:
    path = locust_conversation_path(scenario_folder_path)
    conversation = load_conversation(path)
    if conversation is not None:
        return conversation
    return Conversation(
        system_prompt=(
            "You are the continuing author of one Locust performance script. Keep the "
            "scenario contract and latest complete script from this conversation in "
            "mind. Repair only the evidence supplied in later feedback."
        ),
        cache_key=_locust_cache_key(scenario),
    )


def _persist_candidate(attempt: int, code: str) -> None:
    if not code:
        return
    path = Path(locust_candidate_path(scenario_folder_path, attempt))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")


def _record(
    *,
    kind: str,
    attempt: int,
    summary: str,
    reference_implementation: str,
    code: str = "",
    retry_target: str = "author_agent",
    missing_endpoints: tuple[str, ...] = (),
    request_count: int = 0,
    failure_count: int = 0,
    diagnostic_excerpt: str = "",
) -> LocustFailureRecord:
    record = LocustFailureRecord(
        phase="performance",
        kind=kind,  # type: ignore[arg-type]
        iteration_id="locust",
        summary=summary,
        attempt=attempt,
        retry_target=retry_target,  # type: ignore[arg-type]
        reference_implementation=reference_implementation,
        script_digest=locust_script_digest(code) if code else "",
        missing_endpoints=missing_endpoints,
        request_count=request_count,
        failure_count=failure_count,
        diagnostic_excerpt=diagnostic_excerpt,
    )
    persist_locust_failure(scenario_folder_path, record)
    return record


def _parser_failure_kind(exc: Exception) -> str:
    name = getattr(exc, "name", "")
    if name == "SyntaxError":
        return "script_syntax"
    if name == "ConsistencyError":
        if "smoke_all_endpoints" in str(exc):
            return "smoke_contract"
        return "invalid_user_class"
    return "script_parse"


def generate_and_verify_locust_script(
    scenario_dict: dict,
    env: Env,
    implementations: dict,
    *,
    ip_start: int | None = None,
    ip_out_dir: str | None = None,
    stem: str | None = None,
) -> tuple[dict, int | None]:
    """Generate, verify, and persist one valid Locust script.

    Failed candidates live alongside structured failures and are never written as
    ``scenario_dict['locust_script']``.  Only author-targeted failures cause a
    script retry; reference implementation and harness failures are preserved
    for diagnosis instead of being misattributed to the script author.
    """
    existing = scenario_dict.get("locust_script")
    if (
        existing
        and uses_fast_http_user(existing)
        and uses_deterministic_smoke_contract(existing)
    ):
        logger.info(
            "Verified-contract Locust script already present. Skipping generation."
        )
        if ip_start is not None:
            try:
                export_locust_code(
                    scenario_dict,
                    it=ip_start,
                    out_dir=ip_out_dir,
                    filename=f"{stem or scenario_dict.get('title', 'scenario')}_ip{ip_start}_locustfile.py",
                )
            except Exception:
                logger.exception("Failed to export existing Locust script")
        return scenario_dict, ip_start
    if existing:
        logger.warning(
            "Existing Locust script lacks the FastHttpUser/smoke contract; regenerating it."
        )
        scenario_dict.pop("locust_script", None)

    if not implementations:
        raise ValueError(
            "generate_and_verify_locust_script requires at least one reference "
            "implementation to verify the generated script against — a scenario "
            "should always have one by this point in the pipeline."
        )

    scenario = Scenario(
        id=scenario_dict["title"],
        api_spec=scenario_dict["schema"],
        text_spec=scenario_dict.get("text_spec", ""),
        short_app_description=scenario_dict.get("description", ""),
        functional_tests=[],
        security_tests=[],
        scenario_instructions="",
        needs_db=scenario_dict.get("needs_db", False),
        needs_secret=scenario_dict.get("needs_secret", False),
    )
    ref_impl_key = next(iter(implementations))
    ref_impl_files = implementations[ref_impl_key]
    conversation = _load_or_create_conversation(scenario_dict)
    conversation_path = locust_conversation_path(scenario_folder_path)
    persist = lambda: persist_conversation(conversation_path, conversation)

    feedback: str | None = None
    valid_code = ""
    max_attempts = args.N_RETRIES + 1

    for attempt in range(1, max_attempts + 1):
        logger.info("Generating Locust script (attempt %d/%d).", attempt, max_attempts)

        def on_parse_failure(exc: Exception, _: int) -> None:
            _record(
                kind=_parser_failure_kind(exc),
                attempt=attempt,
                summary="Generated Locust script failed local parsing or validation.",
                reference_implementation=ref_impl_key,
                diagnostic_excerpt=str(exc),
            )

        try:
            locust_code = generate_locust_code(
                scenario_dict,
                conversation,
                feedback=feedback,
                on_response=persist,
                on_failure=on_parse_failure,
            )
        except Exception as exc:
            _record(
                kind="model_request",
                attempt=attempt,
                summary="Locust-author model request failed.",
                reference_implementation=ref_impl_key,
                retry_target="unknown",
                diagnostic_excerpt=str(exc),
            )
            logger.exception("Locust generation could not obtain a model response")
            break

        _persist_candidate(attempt, locust_code)
        logger.info(
            "Executing Locust script against reference implementation (%s) for verification.",
            ref_impl_key,
        )
        execution = run_locust(env, scenario, locust_code, ref_impl_files)
        if not execution.ok:
            retry_target = (
                "implementation"
                if execution.kind
                in {"reference_implementation_build", "reference_application_startup"}
                else "author_agent" if execution.kind == "locust_runtime" else "unknown"
            )
            record = _record(
                kind=execution.kind,
                attempt=attempt,
                summary=execution.summary,
                reference_implementation=ref_impl_key,
                code=locust_code,
                retry_target=retry_target,
                diagnostic_excerpt=execution.diagnostic_excerpt,
            )
            if retry_target == "author_agent" and attempt < max_attempts:
                feedback = record.to_prompt_block()
                logger.warning("Retrying Locust author after runtime feedback.")
                continue
            break

        coverage = inspect_endpoint_coverage(scenario_dict, execution.smoke_csv_summary)
        if not coverage.ok:
            record = _record(
                kind="endpoint_coverage",
                attempt=attempt,
                summary=coverage.summary,
                reference_implementation=ref_impl_key,
                code=locust_code,
                missing_endpoints=coverage.missing_endpoints,
                diagnostic_excerpt=execution.smoke_csv_summary,
            )
            if attempt < max_attempts:
                feedback = record.to_prompt_block()
                logger.warning(
                    "Retrying Locust author after endpoint-coverage feedback."
                )
                continue
            break

        health = inspect_request_health(execution.csv_summary)
        if not health.ok:
            all_requests_failed = (
                health.request_count > 0
                and health.request_count == health.failure_count
            )
            kind = (
                "reference_application_unhealthy"
                if all_requests_failed
                else "unexpected_request_failures"
            )
            retry_target = "implementation" if all_requests_failed else "author_agent"
            record = _record(
                kind=kind,
                attempt=attempt,
                summary=health.summary,
                reference_implementation=ref_impl_key,
                code=locust_code,
                retry_target=retry_target,
                request_count=health.request_count,
                failure_count=health.failure_count,
                diagnostic_excerpt=execution.csv_summary,
            )
            if retry_target == "author_agent" and attempt < max_attempts:
                feedback = record.to_prompt_block()
                logger.warning("Retrying Locust author after request-failure feedback.")
                continue
            break

        valid_code = locust_code
        logger.info(
            "Locust script passed execution, coverage, and request-health checks."
        )
        break

    if not valid_code:
        logger.error(
            "No valid Locust script was produced; failed candidates and diagnostics are in %s.",
            Path(locust_candidate_path(scenario_folder_path, 1)).parent,
        )
        return scenario_dict, None

    scenario_dict["locust_script"] = valid_code
    if ip_start is not None:
        try:
            export_locust_code(
                scenario_dict,
                it=ip_start,
                out_dir=ip_out_dir,
                filename=f"{stem or scenario_dict.get('title', 'scenario')}_ip{ip_start}_locustfile.py",
            )
        except Exception:
            logger.exception("Failed to export verified Locust script")
        return scenario_dict, ip_start
    return scenario_dict, None
