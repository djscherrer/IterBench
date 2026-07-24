import json
import os

from workspace.scenario_builder_paths import (
    latest_scenario_snapshot_path,
    read_results,
    tasklist_path,
    write_results,
)
import templates
from config import args, logger, reasoning_model, scenario_folder_path
from export.render import export_scenario_code
from utils import AgentException, agentic_loop, visualize_baxbench_eval
from llm import Conversation, Response


def parse_verdict(conversation: Conversation) -> bool:
    """Parses the boolean verdict from the conversation."""
    verdict = conversation.responses[-1].text.strip().lower()
    if verdict == "yes":
        return True
    elif verdict == "no":
        return False
    raise AgentException("ParseError", "Could not parse verdict from response")


def verify_tests() -> None:
    """Verifies the functional tests against the scenario specification."""
    logger.info("verifying tests")

    verified_scenario_file_path = os.path.join(
        scenario_folder_path, "spec", "scenario_iv.json"
    )
    if not os.path.exists(verified_scenario_file_path):
        latest_scenario_file = latest_scenario_snapshot_path(scenario_folder_path)

        with open(latest_scenario_file, "r") as file:
            scenario = json.load(file)

        logger.info(f"loaded scenario file: {latest_scenario_file}")

        # verify each test
        valid_tests = []
        valid_specs = []
        valid_names = []
        for i, (test_code, test_spec, test_name) in enumerate(
            zip(
                scenario["functional_tests_code"],
                scenario["tests_spec"],
                scenario["functional_tests_names"],
            )
        ):
            prompt = templates.verify_test.format(
                scenario_title=scenario["title"],
                scenario_description=scenario["description"],
                scenario_openapi=scenario["schema"],
                header_code=scenario["header_code"],
                test_code=test_code,
            )

            conversation = Conversation().add_message(
                Response(role="user", text=prompt)
            )
            response = reasoning_model.generate(
                conversation,
                temperature=0,
                purpose=f"verify_functional_tests: verifying functional test {test_name}",
            )
            conversation.add_message(response)

            is_sound = agentic_loop(
                conversation,
                parse_verdict,
                args.N_RETRIES,
                f"verifying {scenario['functional_tests_names'][i]}",
                "Respond with 'yes' or 'no'.",
            )

            if is_sound:
                logger.info(f"{scenario['functional_tests_names'][i]} is sound")
                valid_tests.append(test_code)
                valid_specs.append(test_spec)
                valid_names.append(test_name)
            else:
                logger.warning(
                    f"{scenario['functional_tests_names'][i]} does not conform to the scenario specification"
                )
                # if args.debug:
                #     conversation.add_message(Response(role="user", text="Why not?"))
                #     response = reasoning_model.generate(
                #         conversation,
                #         temperature=0,
                #         purpose=f"verify_functional_tests: explaining why functional test {test_name} is not sound",
                #     )
                #     conversation.add_message(response)
                #     logger.info(response.text)

        filtered_tests = []
        filtered_specs = []
        filtered_names = []

        filtered_results = {}

        results_root = os.path.join(scenario_folder_path, "results")
        result_dirs = [
            d
            for d in (os.listdir(results_root) if os.path.isdir(results_root) else [])
            if d[:2] in ("it", "iu", "iw") and d[2:].isdigit()
        ]
        if result_dirs:
            latest_tag_dir = sorted(result_dirs, key=lambda d: (d[:2], int(d[2:])))[-1]
            latest_tag, latest_n = latest_tag_dir[:2], int(latest_tag_dir[2:])
            full_results = read_results(scenario_folder_path, latest_tag, latest_n)

            with open(tasklist_path(scenario_folder_path), "r") as file:
                tasklist = json.load(file)

            # do further correctness filtering through a notion of sorted majority voting elim
            # violation_impl_j = sum_test_i (I{test_i_failed} * #impls_that_pass_test_i/#impls)
            violation_scores = {k: 0.0 for k in tasklist.keys()}
            for test_name, test_results in full_results.items():
                if test_name in valid_names:
                    failing_impls = [
                        key
                        for key, result in test_results.items()
                        if result["status"] != "passed"
                    ]
                    fraction_passing_impls = 1 - len(failing_impls) / len(test_results)
                    logger.info(
                        f"{test_name=}: {failing_impls=}, {fraction_passing_impls=}"
                    )
                    for key in failing_impls:
                        violation_scores[key] += fraction_passing_impls

            logger.info(f"Violation scores: {violation_scores}")

            # remove failing tests of best_impl where best_impl = argmin_impl_j violation_impl_j
            argmin_key = min(violation_scores, key=lambda k: violation_scores[k])
            logger.info(f"Remove failing tests of best impl {argmin_key}")

            for i, test_name in enumerate(valid_names):
                if full_results[test_name][argmin_key]["status"] == "passed":
                    filtered_tests.append(valid_tests[i])
                    filtered_specs.append(valid_specs[i])
                    filtered_names.append(valid_names[i])
                    filtered_results[test_name] = full_results[test_name]
                else:
                    logger.warning(f"Removed failing test {test_name}")

        scenario["functional_tests_code"] = filtered_tests
        scenario["tests_spec"] = filtered_specs
        scenario["functional_tests_names"] = filtered_names

        # save post-verification results, subset of cols of iu* results
        write_results(scenario_folder_path, "iv", "", filtered_results)

        visualize_baxbench_eval(
            filtered_results, "", iv=True, x_axis_labels=scenario["all_tests_names"]
        )

        # save post-verification scenario
        os.makedirs(os.path.dirname(verified_scenario_file_path), exist_ok=True)
        with open(verified_scenario_file_path, "w") as file:
            json.dump(scenario, file, indent=4)

        export_scenario_code(scenario, iv=True)
