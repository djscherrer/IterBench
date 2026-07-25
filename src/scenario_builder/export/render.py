import os
import re

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


_SHAPE_IMPORT_LINE = "from _baxbench_shape import BaxbenchShape, baxbench_wait_time"
_LOCUST_IMPORT_RE = re.compile(r"^from\s+locust(?:\.\w+)*\s+import[^\n]*$", re.MULTILINE)
_WAIT_TIME_ASSIGN_RE = re.compile(r"^([ \t]*)wait_time\s*=.*$", re.MULTILINE)
_USER_CLASS_RE = re.compile(r"^class\s+\w+\s*\(\s*FastHttpUser\s*\):.*$", re.MULTILINE)
_SHAPE_CLASS_PRESENT_RE = re.compile(r"class\s+\w+\s*\(\s*BaxbenchShape\s*\)")


def add_baxbench_shape_wiring(locust_code: str) -> str:
    """Wires a verified Locust script into load_bench's standardized load-shape
    and pacing contract (``BaxbenchShape`` / ``baxbench_wait_time``), so an
    exported scenario is ready to run for real without manual editing.

    Deliberately not part of generation/verification: ``_baxbench_shape`` is
    infrastructure ``load_bench`` stages next to a locustfile at real bench
    time (see ``load_bench/locust_run.py``'s ``_stage_baxbench_shapes``), and
    neither of scenario_builder's own verification passes stage it -- the
    import would break both if it were present during authoring, and the
    ``BaxbenchShape`` class would hijack Locust's ramp control away from our
    controlled verification runs. So this rewrite only happens here, at
    export time, once a script is done being authored and verified.
    """
    if _SHAPE_CLASS_PRESENT_RE.search(locust_code):
        # Already wired (e.g. re-exporting an already-processed script);
        # don't double-inject.
        return locust_code

    code = locust_code

    # 1) Import, after the last `from locust...` line (there's always at
    #    least the `from locust import ...` every verified script is
    #    required to have -- see generate.uses_fast_http_user -- and
    #    sometimes also e.g. `from locust.exception import StopUser`;
    #    inserting after the last one keeps them grouped together).
    matches = list(_LOCUST_IMPORT_RE.finditer(code))
    if matches:
        insert_at = matches[-1].end()
        code = code[:insert_at] + "\n" + _SHAPE_IMPORT_LINE + code[insert_at:]
    else:
        code = _SHAPE_IMPORT_LINE + "\n" + code

    # 2) wait_time: replace an existing class-level assignment in place, or
    #    insert one as the User class's first body line if none exists.
    if _WAIT_TIME_ASSIGN_RE.search(code):
        code = _WAIT_TIME_ASSIGN_RE.sub(
            lambda m: f"{m.group(1)}wait_time = baxbench_wait_time()", code, count=1
        )
    else:
        user_match = _USER_CLASS_RE.search(code)
        if user_match:
            code = (
                code[: user_match.end()]
                + "\n    wait_time = baxbench_wait_time()"
                + code[user_match.end() :]
            )

    # 3) Shape class, at module level.
    code = code.rstrip() + "\n\n\nclass Shape(BaxbenchShape):\n    pass\n"

    return code
