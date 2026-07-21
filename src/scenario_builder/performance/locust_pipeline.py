import json
import os

from config import args, logger
from performance.generate import generate_locust_code
from export.render import export_locust_code
from performance.verify import check_endpoint_coverage, run_locust
from env.base import Env
from llm import Conversation
from scenarios.base import Scenario


def generate_and_verify_locust_script(
    scenario_dict: dict,
    env: Env,
    implementations: dict,
    *,
    ip_start: int | None = None,
    ip_out_dir: str | None = None,
    stem: str | None = None,
) -> tuple[dict, int | None]:
    """Generates a Locust script, verifies it, and updates the scenario."""

    if scenario_dict.get("locust_script"):
        logger.info("Locust script already present. Skipping generation.")
        if ip_start is not None:
            # still export the existing script as an ip snapshot
            try:
                export_locust_code(
                    scenario_dict,
                    it=ip_start,
                    out_dir=ip_out_dir,
                    filename=f"{stem or scenario_dict.get('title', 'scenario')}_ip{ip_start}_locustfile.py",
                )
            except Exception:
                # don't fail the pipeline if exporting fails
                logger.exception("Failed to export existing locust script")
        return scenario_dict, ip_start

    if not implementations:
        raise ValueError(
            "generate_and_verify_locust_script requires at least one reference "
            "implementation to verify the generated script against — a scenario "
            "should always have one by this point in the pipeline."
        )

    # Build a real Scenario object from the raw dict — run_locust()/ContainerRunner
    # need a Scenario instance (mainly for its id), not the dict representation.
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

    ref_impl_key = list(implementations.keys())[0]
    ref_impl_files = implementations[ref_impl_key]

    max_attempts = args.N_RETRIES + 1
    feedback = None
    locust_code = ""

    for attempt in range(1, max_attempts + 1):
        logger.info(f"Generating Locust script (attempt {attempt}/{max_attempts}).")
        locust_code = generate_locust_code(
            scenario_dict,
            Conversation(),
            feedback=feedback,
        )

        logger.info(
            f"Executing Locust script against reference implementation ({ref_impl_key}) for verification."
        )
        success, csv_summary = run_locust(env, scenario, locust_code, ref_impl_files)

        if not success:
            logger.warning("Locust execution trace encountered failures.")

        is_valid, coverage_reason = (
            check_endpoint_coverage(scenario_dict, csv_summary) if success else (False, csv_summary)
        )
        if is_valid:
            logger.info("Locust script verified: %s", coverage_reason)
        else:
            logger.warning("Locust script failed endpoint coverage check: %s", coverage_reason)

        if success and is_valid:
            logger.info("Locust script passed execution and verification checks.")
            break

        feedback = (
            "The previous Locust script did not pass checks.\n"
            f"Execution success: {success}.\n"
            f"Endpoint coverage check: {coverage_reason}\n"
            "Execution output / CSV summary:\n"
            f"{csv_summary}\n"
            "Please fix endpoint coverage, runtime errors, and request construction."
        )

        if attempt < max_attempts:
            logger.warning("Retrying Locust script generation with failure feedback.")
        else:
            logger.warning(
                "Maximum Locust retries reached. Keeping last generated script for manual refinement."
            )

    scenario_dict["locust_script"] = locust_code
    # Export per-attempt (ip series) if requested.
    if ip_start is not None:
        ip_idx = ip_start + (attempt - 1)
        try:
            export_locust_code(
                scenario_dict,
                it=ip_idx,
                out_dir=ip_out_dir,
                filename=f"{stem or scenario_dict.get('title', 'scenario')}_ip{ip_idx}_locustfile.py",
            )
        except Exception:
            logger.exception("Failed to export locust script")
        return scenario_dict, ip_idx

    return scenario_dict, None
