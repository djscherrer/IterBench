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
    review_locust_code,
    uses_fast_http_user,
)
from performance.verify import (
    build_load_review_report,
    inspect_endpoint_coverage,
    run_smoke_only,
    run_weighted_load_sweep,
)

from env.base import Env
from llm import Conversation
from scenario_builder.conversation_store import load_conversation, persist_conversation
from scenarios.base import Scenario
from workspace.scenario_builder_paths import (
    legacy_lowcost_conversation_path,
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
    if conversation is not None and conversation.responses:
        return conversation
    legacy_conversation = load_conversation(
        legacy_lowcost_conversation_path(scenario_folder_path)
    )
    if legacy_conversation is not None and legacy_conversation.responses:
        # Keep the old artifact untouched, but make future reads and writes use
        # the canonical locust.json filename.
        persist_conversation(path, legacy_conversation)
        logger.info("Migrated Locust conversation to %s", path)
        return legacy_conversation
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


def ensure_locust_conversation(scenario: dict) -> Conversation:
    """Create the durable performance-author thread on phase entry."""
    conversation = _load_or_create_conversation(scenario)
    persist_conversation(locust_conversation_path(scenario_folder_path), conversation)
    return conversation


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
    failing_endpoints: tuple[str, ...] = (),
    failing_by_implementation: dict[str, tuple[str, ...]] | None = None,
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
        failing_endpoints=failing_endpoints,
        failing_by_implementation=failing_by_implementation or {},
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

    Two stages per candidate script:

    1. **Smoke gate** (cheap, deterministic): every task fired once against
       the primary reference implementation only. Blocks on parse/build/
       runtime errors and on any OpenAPI endpoint never reached at all;
       failures here retry the author with concrete feedback.
    2. **Load review** (real load, once per candidate that clears stage 1):
       a weighted load pass (100 users, no cluster -- one backend(+db)
       container per implementation, sequential) against *every* reference
       implementation. Stats, distinct failure messages, and unhandled
       exceptions from all of them are handed back to the author, who
       decides whether the script itself needs a change; comparing across
       implementations lets it tell a script bug (the same problem
       everywhere) from an implementation bug (isolated to one), which is
       not the script's job to work around.

    Failed candidates live alongside structured failures and are never written as
    ``scenario_dict['locust_script']``.  Only author-targeted failures cause a
    script retry; reference implementation and harness failures are preserved
    for diagnosis instead of being misattributed to the script author.
    """
    # Persist the author artifact on phase entry, including for scenarios that
    # already have a verified script and therefore need no model call.
    conversation = ensure_locust_conversation(scenario_dict)
    conversation_path = locust_conversation_path(scenario_folder_path)
    persist = lambda: persist_conversation(conversation_path, conversation)

    existing = scenario_dict.get("locust_script")
    if existing and uses_fast_http_user(existing):
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
            "Existing Locust script lacks the FastHttpUser contract; regenerating it."
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
    max_attempts = args.N_RETRIES + 1

    def make_on_parse_failure(attempt: int):
        def on_parse_failure(exc: Exception, _: int) -> None:
            _record(
                kind=_parser_failure_kind(exc),
                attempt=attempt,
                summary="Generated Locust script failed local parsing or validation.",
                reference_implementation=ref_impl_key,
                diagnostic_excerpt=str(exc),
            )

        return on_parse_failure

    feedback: str | None = None
    # Set when a candidate is already in hand (an author revision produced by
    # the load-review step below) and should go straight to stage-1
    # validation on the next loop pass, instead of prompting the model again.
    pending_code: str | None = None
    valid_code = ""

    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        logger.info("Verifying Locust script (attempt %d/%d).", attempt, max_attempts)
        on_parse_failure = make_on_parse_failure(attempt)

        if pending_code is None:
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
        else:
            locust_code = pending_code
            pending_code = None
        _persist_candidate(attempt, locust_code)

        # Stage 1: deterministic smoke gate against the primary implementation.
        logger.info("Stage 1: smoke-testing Locust script against %s.", ref_impl_key)
        smoke = run_smoke_only(env, scenario, locust_code, ref_impl_files)
        if not smoke.ok:
            retry_target = (
                "implementation"
                if smoke.kind
                in {"reference_implementation_build", "reference_application_startup"}
                else "author_agent" if smoke.kind == "locust_runtime" else "unknown"
            )
            record = _record(
                kind=smoke.kind,
                attempt=attempt,
                summary=smoke.summary,
                reference_implementation=ref_impl_key,
                code=locust_code,
                retry_target=retry_target,
                diagnostic_excerpt=smoke.diagnostic_excerpt,
            )
            if retry_target == "author_agent" and attempt < max_attempts:
                feedback = record.to_prompt_block()
                logger.warning("Retrying Locust author after stage-1 runtime feedback.")
                continue
            break

        coverage = inspect_endpoint_coverage(scenario_dict, smoke.smoke_csv_summary)
        if not coverage.ok:
            record = _record(
                kind="endpoint_coverage",
                attempt=attempt,
                summary=coverage.summary,
                reference_implementation=ref_impl_key,
                code=locust_code,
                missing_endpoints=coverage.missing_endpoints,
                diagnostic_excerpt=smoke.smoke_csv_summary,
            )
            if attempt < max_attempts:
                feedback = record.to_prompt_block()
                logger.warning("Retrying Locust author after stage-1 coverage feedback.")
                continue
            break

        # Stage 2: real weighted load against every reference implementation.
        logger.info(
            "Stage 2: running weighted load pass against %d reference implementation(s).",
            len(implementations),
        )
        sweep = run_weighted_load_sweep(env, scenario, locust_code, implementations)
        for impl_key, result in sweep.items():
            if not result.ok:
                # Informational only: an implementation that couldn't even be
                # evaluated is that implementation's problem, not grounds to
                # retry the author on its own. It still reaches the author
                # (via the review report below) whenever at least one other
                # implementation did produce results to compare against.
                _record(
                    kind=result.kind,
                    attempt=attempt,
                    summary=result.summary,
                    reference_implementation=impl_key,
                    code=locust_code,
                    retry_target="implementation",
                    diagnostic_excerpt=result.diagnostic_excerpt,
                )

        if not any(result.ok for result in sweep.values()):
            _record(
                kind="load_review_unavailable",
                attempt=attempt,
                summary=(
                    "No reference implementation could be evaluated under weighted "
                    "load; accepting the script on stage-1 evidence alone."
                ),
                reference_implementation=ref_impl_key,
                code=locust_code,
                retry_target="infrastructure",
            )
            logger.warning(
                "Stage 2 could not be evaluated against any implementation; "
                "accepting the script on stage-1 evidence alone."
            )
            valid_code = locust_code
            break

        # Only worth a review round-trip if at least one implementation
        # actually produced a failure or an exception to look at; a clean
        # sweep has nothing to review, and asking anyway just invites an
        # unprompted, unjustified edit to a script that already works.
        has_findings = any(
            result.ok and (result.failures_csv or result.exceptions_csv)
            for result in sweep.values()
        )
        if not has_findings:
            logger.info(
                "Stage 2 completed cleanly across all evaluated implementations; "
                "accepting the script without a review round-trip."
            )
            valid_code = locust_code
            break

        if attempt >= max_attempts:
            # No budget left to re-verify a revision even if the author wanted
            # to make one, so don't offer it the choice.
            logger.info(
                "Stage 2 surfaced findings but no retry budget remains for a "
                "review round-trip; accepting the script as-is."
            )
            valid_code = locust_code
            break

        report = build_load_review_report(sweep)
        try:
            reviewed_code = review_locust_code(
                scenario_dict,
                conversation,
                report,
                on_response=persist,
                on_failure=on_parse_failure,
            )
        except Exception as exc:
            _record(
                kind="model_request",
                attempt=attempt,
                summary="Locust-author model request failed during load review.",
                reference_implementation=ref_impl_key,
                retry_target="unknown",
                diagnostic_excerpt=str(exc),
            )
            logger.exception("Locust load review could not obtain a model response")
            valid_code = locust_code
            break

        if reviewed_code is None:
            logger.info("Author found nothing to fix after load review.")
            valid_code = locust_code
            break

        _record(
            kind="load_review_revision",
            attempt=attempt,
            summary=(
                "Author revised the script after reviewing cross-implementation "
                "load results."
            ),
            reference_implementation=ref_impl_key,
            code=reviewed_code,
            retry_target="author_agent",
        )
        logger.info("Author revised the script after load review; re-verifying.")
        pending_code = reviewed_code

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
