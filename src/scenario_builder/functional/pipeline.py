# flake8: noqa F821
# mypy: disable-error-code=name-defined
import json
import os
import pickle
from collections import defaultdict
from pathlib import Path, PosixPath

from export.render import export_scenario_code
from workspace.scenario_builder_paths import (
    implementation_path,
    load_code,
    read_results,
    results_summary_path,
    save_code,
    snapshot_path,
    spec_path,
    tasklist_path,
    write_results,
)
from config import RESULTS_DIR, args, build_tasks, logger, scenario_folder_path
from functional.generate import generate_tests_code, generate_tests_spec
from functional.iterate import iterate_blackbox, iterate_whitebox
from functional.conversations import ImplementationConversationStore
from utils import (
    clean_code,
    deep_update,
    test_and_evaluate_baxbench,
    visualize_baxbench_eval,
)
from llm import Conversation


def ensure_results_cover_keys(full_results: dict, keys: list[str]) -> None:
    """Ensure each test has a result entry for every implementation key."""
    for test_name, test_results in full_results.items():
        if not isinstance(test_results, dict):
            continue
        for key in keys:
            if key not in test_results:
                test_results[key] = {
                    "status": "exception",
                    "container_logs": (
                        "No test result entry found for this implementation key. "
                        "Likely missing/partial cached results."
                    ),
                    "test_logs": "",
                }


def augment_header_functional_test_signatures(header_code: str, test_code: str) -> str:
    """Augment the shared header with imports required by generated test code.

    Function annotations are evaluated when the exported scenario module is
    executed.  The generation prompt asks for ``AppInstance`` explicitly, but
    model output occasionally omits its import; make the export self-contained
    before calling ``exec(code, globals())``.
    """
    if test_code is None:
        return header_code
    if (
        "AppInstance" in header_code or "AppInstance" in test_code
    ) and "from scenarios.base import AppInstance" not in header_code:
        header_code = "from scenarios.base import AppInstance\n" + header_code
    if "SCENARIO_FILE_PATH" in header_code or "SCENARIO_FILE_PATH" in test_code:
        header_code = "from scenario_files import SCENARIO_FILE_PATH\n" + header_code
    if "place_file_on_docker" in header_code or "place_file_on_docker" in test_code:
        header_code = "from exploits import place_file_on_docker\n" + header_code
    return header_code


def generate_and_iterate_tests() -> None:
    """Generates and iterates on functional tests for the scenario."""
    logger.info("generating tests")
    logger.info(f"Scenario folder path: {scenario_folder_path}")

    iu0_path = snapshot_path(scenario_folder_path, "iu", 0)
    if os.path.exists(iu0_path):
        with open(iu0_path, "r", encoding="utf-8") as file:
            scenario = json.load(file)
        normalized_header = clean_code(
            augment_header_functional_test_signatures(
                scenario["header_code"], "\n".join(scenario["functional_tests_code"])
            )
        )
        header_changed = normalized_header != scenario["header_code"]
        if header_changed:
            scenario["header_code"] = normalized_header
            with open(iu0_path, "w", encoding="utf-8") as file:
                json.dump(scenario, file, indent=4)
        code = export_scenario_code(scenario, write=header_changed)
    else:
        with open(spec_path(scenario_folder_path), "r", encoding="utf-8") as file:
            scenario = json.load(file)
            conversation = Conversation()
            scenario["tests_spec"] = generate_tests_spec(scenario, conversation)
            (
                scenario["header_code"],
                scenario["functional_tests_code"],
                scenario["functional_tests_names"],
            ) = generate_tests_code(scenario, conversation)
            scenario["header_code"] = clean_code(
                augment_header_functional_test_signatures(
                    scenario["header_code"],
                    "\n".join(scenario["functional_tests_code"]),
                )
            )
            scenario["all_tests_names"] = scenario["functional_tests_names"]
            assert (
                len(scenario["functional_tests_code"])
                == len(scenario["functional_tests_names"])
                == len(scenario["tests_spec"])
            ), "Mismatch in functional tests specs vs code"
        with open(iu0_path, "w") as file:
            json.dump(scenario, file, indent=4)

        code = export_scenario_code(scenario)

    exec(code, globals())

    tasklist_file = tasklist_path(scenario_folder_path)
    if os.path.exists(tasklist_file):
        with open(tasklist_file, "r") as file:
            task_dict = json.load(file)
    else:
        from tasks import TaskHandler

        task_list = build_tasks(SCENARIO)
        TaskHandler(
            task_list, results_dir=RESULTS_DIR, max_concurrent_runs=None
        ).run_generation(
            batch_size=1, max_retries=20, base_delay=1.0, max_delay=128.0, force=True
        )
        task_dict = {
            f"{task.env.language} {task.env.framework} {task.model}": str(
                task.get_code_dir(RESULTS_DIR, 0)
            )
            for task in task_list
        }
        with open(tasklist_file, "w") as file:
            json.dump(task_dict, file, indent=4)

    implementations = {}
    it0_impl_path = implementation_path(scenario_folder_path, "it", 0)
    if os.path.exists(it0_impl_path):
        with open(it0_impl_path, "r") as file:
            raw_implementations = json.load(file)
            implementations = {
                k: {PosixPath(path): code for path, code in v.items()}
                for k, v in raw_implementations.items()
            }
    else:
        for key, code_dir_str in task_dict.items():
            implementations[key] = load_code(code_dir_str)
        with open(it0_impl_path, "w") as file:
            json.dump(
                {
                    k: {str(path): code for path, code in v.items()}
                    for k, v in implementations.items()
                },
                file,
                indent=4,
            )

    if os.path.exists(results_summary_path(scenario_folder_path, "it", 0)):
        full_results = read_results(scenario_folder_path, "it", 0)
    else:
        full_results = test_and_evaluate_baxbench(SCENARIO)
        write_results(scenario_folder_path, "it", 0, full_results)

    ensure_results_cover_keys(full_results, list(task_dict.keys()))

    visualize_baxbench_eval(
        full_results,
        0,
        highlight_x=scenario["functional_tests_names"],
        x_axis_labels=scenario["all_tests_names"],
    )

    keys = list(task_dict.keys())
    implementation_threads = ImplementationConversationStore(scenario_folder_path)
    for it in range(1, args.N_SOL_STEPS + 1):
        it_impl_path = implementation_path(scenario_folder_path, "it", it)
        it_results_exists = os.path.exists(results_summary_path(scenario_folder_path, "it", it))
        if os.path.exists(it_impl_path) and it_results_exists:
            with open(it_impl_path, "r") as file:
                raw_implementations = json.load(file)
                implementations = {
                    k: {PosixPath(path): code for path, code in v.items()}
                    for k, v in raw_implementations.items()
                }

            deep_update(full_results, read_results(scenario_folder_path, "it", it))
            ensure_results_cover_keys(full_results, keys)
        else:
            modified_implementations = []
            for key in keys:
                model_results = {
                    test: test_results[key]
                    for test, test_results in full_results.items()
                    if key in test_results
                }
                if not model_results:
                    logger.warning(f"No test execution results found for {key}, skipping blackbox iteration.")
                    continue
                new_implementation = iterate_blackbox(
                    scenario,
                    key,
                    model_results,
                    implementations[key],
                    iteration=it,
                    thread_store=implementation_threads,
                )
                if new_implementation != implementations[key]:
                    implementations[key] = new_implementation
                    save_code(implementations[key], task_dict[key])
                    modified_implementations.append(key)
                else:
                    logger.info(f"Implementation {it} remains unchanged for {key}")
            if modified_implementations:
                logger.info(
                    f"Testing re-implementation {it} for {', '.join(modified_implementations)}"
                )

                deep_update(
                    full_results,
                    test_and_evaluate_baxbench(
                        SCENARIO, [key.split()[-1] for key in modified_implementations]
                    ),
                )
                ensure_results_cover_keys(full_results, keys)

            with open(it_impl_path, "w") as file:
                json.dump(
                    {
                        k: {str(path): code for path, code in v.items()}
                        for k, v in implementations.items()
                    },
                    file,
                    indent=4,
                )

            write_results(scenario_folder_path, "it", it, full_results)

            visualize_baxbench_eval(
                full_results,
                it,
                highlight_y=modified_implementations,
                x_axis_labels=scenario["all_tests_names"],
            )
            if not modified_implementations:
                logger.info("No modified implementations, blackbox iteration converged")
                break

    # this is used to cache the verdicts of the functional tests
    # (test, implementation) -> verdict
    # (test, "all") -> aggregated verdict
    # this is used to avoid extra LLM calls for cases that haven't changed across iterations
    verdict_cache: defaultdict[tuple[str, str], str] = defaultdict(str)
    for it in range(1, args.N_TEST_STEPS + 1):
        modified_tests = []
        modified_implementations = []
        modified_header = False

        iu_it_path = snapshot_path(scenario_folder_path, "iu", it)
        if os.path.exists(iu_it_path):
            with open(iu_it_path, "r") as file:
                scenario = json.load(file)
            modified_header = True  # s.t. the iteration doesn't stop prematurely since the else block below is skipped
        else:
            i = 0
            while i < len(scenario["functional_tests_names"]):
                test = scenario["functional_tests_names"][i]
                if test not in full_results:
                    i += 1
                    continue
                results = full_results[test]
                # if any(result["status"] == "failed" for result in results.values()):
                logger.info(f"Iterating functional tests for {test}")

                verdict, test_code, test_spec, modified_implementations = (
                    iterate_whitebox(
                        i,
                        scenario,
                        test,
                        results,
                        implementations,
                        verdict_cache,
                        iteration=it,
                        thread_store=implementation_threads,
                    )
                )
                if verdict == 0:  # cached verdict
                    pass
                elif verdict == 1:  # test is wrong
                    # Reset verdict cache entries for the modified/discarded test
                    for impl_key in keys:
                        verdict_cache[
                            (scenario["functional_tests_names"][i], impl_key)
                        ] = ""
                    verdict_cache[(scenario["functional_tests_names"][i], "all")] = ""
                    modified_tests.append(scenario["functional_tests_names"][i])

                    if test_code:  # test was fixed
                        scenario["functional_tests_code"][i] = test_code
                        scenario["tests_spec"][i] = test_spec
                    else:  # test was discarded
                        modified_tests.append(scenario["functional_tests_names"][i])
                        del scenario["functional_tests_code"][i]
                        del scenario["functional_tests_names"][i]
                        del scenario["tests_spec"][i]
                        continue
                elif (
                    modified_implementations
                ):  # i.e. verdict 2: test is correct PLUS impl changed

                    # reset verdict cache for modified implementations
                    for test_key in scenario["functional_tests_names"]:
                        verdict_cache[(test_key, "all")] = ""
                        for impl_key in modified_implementations:
                            verdict_cache[(test_key, impl_key)] = ""
                    break
                elif verdict == 3:  # more info needed
                    # Reset verdict cache entries for the augmented test
                    for impl_key in keys:
                        verdict_cache[
                            (scenario["functional_tests_names"][i], impl_key)
                        ] = ""
                    verdict_cache[(scenario["functional_tests_names"][i], "all")] = ""
                    scenario["functional_tests_code"][i] = test_code
                    modified_tests.append(scenario["functional_tests_names"][i])
                elif verdict == 4:
                    # Reset all verdict cache entries
                    for test_key in scenario["functional_tests_names"]:
                        for impl_key in keys:
                            verdict_cache[(test_key, impl_key)] = ""
                        verdict_cache[(test_key, "all")] = ""
                    scenario["header_code"] = test_code

                scenario["header_code"] = clean_code(
                    augment_header_functional_test_signatures(
                        scenario["header_code"], test_code
                    )
                )
                i += 1
            with open(iu_it_path, "w") as file:
                json.dump(scenario, file, indent=4)

        # save/load implementations of current iteration
        iu_impl_path = implementation_path(scenario_folder_path, "iu", it)
        if os.path.exists(iu_impl_path):
            with open(iu_impl_path, "r") as file:
                raw_implementations = json.load(file)
                implementations = {
                    k: {PosixPath(path): code for path, code in v.items()}
                    for k, v in raw_implementations.items()
                }
        else:
            for key, code_dir_str in task_dict.items():
                save_code(implementations[key], code_dir_str)
            with open(iu_impl_path, "w") as file:
                json.dump(
                    {
                        k: {str(path): code for path, code in v.items()}
                        for k, v in implementations.items()
                    },
                    file,
                    indent=4,
                )

        # save/load results of current iteration
        if os.path.exists(results_summary_path(scenario_folder_path, "iu", it)):
            deep_update(full_results, read_results(scenario_folder_path, "iu", it))
            ensure_results_cover_keys(full_results, keys)
        else:
            code = export_scenario_code(scenario, it)
            exec(code, globals())

            deep_update(full_results, test_and_evaluate_baxbench(SCENARIO))
            ensure_results_cover_keys(full_results, keys)

            write_results(scenario_folder_path, "iu", it, full_results)

        visualize_baxbench_eval(
            full_results,
            it,
            iu=True,
            highlight_x=modified_tests,
            highlight_y=modified_implementations,
            x_axis_labels=scenario["all_tests_names"],
        )

        if not modified_tests and not modified_implementations and not modified_header:
            logger.info(
                "No modified tests, implementations, or header, whitebox iteration converged"
            )
            break
