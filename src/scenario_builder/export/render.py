import os

import templates
from workspace.scenario_builder_paths import export_path
from config import args, logger, scenario_folder_path


def export_scenario_code(
    scenario: dict,
    it: int = 0,
    iv: bool = False,
    write: bool = True,
    sec: bool = False,
    out_dir: str | None = None,
    filename: str | None = None,
) -> str:
    """Export scenario data to a Python code file.

    Args:
        scenario: Dictionary containing scenario data including:
            - header_code: Import statements and helper code
            - functional_tests_code: List of functional test implementations
            - security_tests_code: List of security test implementations (if sec=True)
            - schema: OpenAPI schema specification
            - text_spec: Text description of the scenario
            - title, description: Scenario metadata
            - needs_db, needs_secret: Resource requirements
            - scenario_instructions: Setup instructions
            - functional_tests_names: List of functional test names
            - security_tests_names: List of security test names (if sec=True)
            - needed_packages: Optional list of required packages
        it: Iteration number for naming (used in _iu{it} or _iw{it} suffix)
        iv: If True, generates an "iv" (verified) version file
        write: If True, writes the code to a file; if False, only returns the code string
        sec: If True, includes security tests in the export

    Returns:
        The generated Python code as a string
    """
    needed_packages_param = ""
    if "needed_packages" in scenario:
        needed_packages_param = f"\n    needed_packages={scenario['needed_packages']},"

    # Locust script (optional). Keep it human-readable in the exported module.
    scenario_locustfile = (scenario.get("locust_script") or "").rstrip()
    if not sec:
        code = templates.scenario_file.format(
            header_code=scenario["header_code"],
            tests_code="\n\n".join(scenario["functional_tests_code"]),
            scenario_openapi=scenario["schema"],
            scenario_textspec=scenario["text_spec"],
            scenario_locustfile=scenario_locustfile,
            scenario_title=scenario["title"],
            scenario_description=scenario["description"],
            scenario_needsdb=scenario["needs_db"],
            scenario_needssecret=scenario["needs_secret"],
            scenario_instructions=scenario["scenario_instructions"],
            scenario_functional_tests_names=", ".join(
                scenario["functional_tests_names"]
            ),
            scenario_security_tests_names="",
            scenario_needed_packages=needed_packages_param,
        )
    else:
        code = templates.scenario_file.format(
            header_code=scenario["header_code"],
            tests_code="\n\n".join(
                scenario["functional_tests_code"] + scenario["security_tests_code"]
            ),
            scenario_openapi=scenario["schema"],
            scenario_textspec=scenario["text_spec"],
            scenario_locustfile=scenario_locustfile,
            scenario_title=scenario["title"],
            scenario_description=scenario["description"],
            scenario_needsdb=scenario["needs_db"],
            scenario_needssecret=scenario["needs_secret"],
            scenario_instructions=scenario["scenario_instructions"],
            scenario_functional_tests_names=", ".join(
                scenario["functional_tests_names"]
            ),
            scenario_security_tests_names=", ".join(scenario["security_tests_names"]),
            scenario_needed_packages=needed_packages_param,
        )

    if out_dir is not None:
        if filename is not None:
            full_path = os.path.join(out_dir, filename)
        elif iv:
            full_path = os.path.join(out_dir, f"{args.scenario}_iv.py")
        elif sec:
            full_path = os.path.join(out_dir, f"{args.scenario}_iw{it}.py")
        else:
            full_path = os.path.join(out_dir, f"{args.scenario}_iu{it}.py")
    elif filename is not None:
        full_path = export_path(scenario_folder_path, filename=filename)
    elif iv:
        full_path = export_path(scenario_folder_path, "iv", "")
    elif sec:
        full_path = export_path(scenario_folder_path, "iw", it)
    else:
        full_path = export_path(scenario_folder_path, "iu", it)

    if not write:
        return code

    with open(full_path, "w") as file:
        file.write(code)

    logger.info(f"Wrote scenario to {full_path}")

    return code


def export_locust_code(
    scenario: dict,
    it: int = 0,
    *,
    out_dir: str | None = None,
    filename: str | None = None,
) -> str:
    """
    Export the Locust script stored in the scenario dict to a Python file.

    Expects `scenario["locust_script"]` to exist.
    """
    out_dir = out_dir or os.path.join(scenario_folder_path, "snapshots", "performance")
    locust_code = (scenario.get("locust_script") or "").strip()
    if not locust_code:
        raise KeyError("Scenario does not contain a non-empty 'locust_script'")

    if filename is None:
        filename = f"{args.scenario}_ip{it}_locustfile.py"

    full_path = os.path.join(out_dir, filename)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(locust_code)

    logger.info(f"Wrote locust script to {full_path}")
    return locust_code
