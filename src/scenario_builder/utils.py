"""Utility functions for AutoBaxBuilder.

This module provides various utility functions including:
- Agentic loop for error recovery
- Code formatting and cleaning
- BaxBench test execution and evaluation
- Visualization of test results
"""

import os
import sys
from collections.abc import Callable

import black
import isort
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns
import templates
from config import (
    RESULTS_DIR,
    args,
    build_tasks,
    logger,
    reasoning_model,
    scenario_folder_path,
)

from llm import Response
from workspace.scenario_builder_paths import record_verdict, results_png_path


class AgentException(Exception):
    """Supports the implementation of fixing errors with an agentic loop"""

    def __init__(self, name, description):
        self.name = name
        self.description = description
        super().__init__(f"Error {name}: {description}")


def agentic_loop(
    conversation,
    f,
    N,
    action,
    format_requirements,
    model_=reasoning_model,
    on_response: Callable[[], None] | None = None,
    on_failure: Callable[[Exception, int], None] | None = None,
    record_verdicts: bool = True,
):
    """
    Execute a retry loop with model-based error recovery.

    This function attempts to execute a validation function up to N times,
    prompting the model to fix errors when they occur.

    Invariant: f(conversation) must either return a valid result or raise an exception
    """

    logger.info(action)

    i = 0
    while i <= N:
        try:
            y = f(conversation)
        except Exception as e:
            logger.warning(e)
            if record_verdicts:
                record_verdict(scenario_folder_path, "Error", str(e))
            if on_failure is not None:
                on_failure(e, i + 1)

            prompt = templates.fix_error.format(
                action=action,
                error=str(e),
                format=format_requirements,
            )
        else:
            logger.info(f"Successful in {action}")
            return y

        if i < N:
            logger.warning("retrying...")
            conversation.add_message(Response(role="user", text=prompt))
            response = model_.generate(
                conversation, temperature=0, purpose=f"utils: agentic loop for {action}"
            )
            conversation.add_message(response)
            if on_response is not None:
                on_response()
        i += 1
    logger.warning(conversation)
    logger.error("aborting...")
    sys.exit(f"Could not recover from error in {action}")


def visualize_baxbench_eval(
    test_results,
    it,
    iu=False,
    iw=False,
    iv=False,
    highlight_x=None,
    highlight_y=None,
    x_axis_labels=None,
):
    """Generate a heatmap visualization of BaxBench test results."""
    data = []

    for test, results in test_results.items():
        for key, result in results.items():
            lang, framework, model_name = key.split()
            data.append([lang + " " + framework, model_name, test, result["status"]])

    df = pd.DataFrame(data, columns=["Framework", "Model", "Test", "Result"])

    df["Framework_Model"] = df["Framework"] + "\n" + df["Model"]
    # Map results to numerical values with a wider range to ensure proper color mapping
    df["Result_Num"] = df["Result"].map({"passed": 2, "exception": 1, "failed": 0})

    df_pivot = df.pivot(index="Framework_Model", columns="Test", values="Result_Num")

    # If x_axis_labels is provided, ensure all expected test cases are present
    if x_axis_labels is not None:
        # Get all unique framework_model combinations
        framework_models = df_pivot.index.tolist()

        # Create a new DataFrame with all expected test cases, ensuring numeric dtype
        full_df_pivot = pd.DataFrame(
            index=framework_models, columns=x_axis_labels, dtype=float
        )

        # Fill in existing data
        for col in df_pivot.columns:
            if col in full_df_pivot.columns:
                full_df_pivot[col] = df_pivot[col]

        # Fill missing columns with NaN (which will appear as white in the heatmap)
        df_pivot = full_df_pivot

    # Use a color palette that ensures exceptions are always yellow
    cmap = sns.color_palette(["#ff4d4d", "#ffcc00", "#33cc33"])  # Red, Yellow, Green

    plt.figure(figsize=(12, 6))
    ax = sns.heatmap(
        df_pivot,
        cmap=cmap,
        cbar=False,
        square=True,
        linewidths=0.5,
        linecolor="black",
        xticklabels=True,
        yticklabels=True,
        annot=False,
        vmin=0,  # Ensure minimum value is 0
        vmax=2,  # Ensure maximum value is 2
        mask=df_pivot.isna(),  # Mask NaN values to show as white
    )

    plt.xticks(rotation=45, ha="right")
    plt.yticks(rotation=0)
    plt.title("Test Results")
    plt.xlabel("Test Cases")
    plt.ylabel("Framework & Model")

    # Add vertical divider between functional and security tests
    if x_axis_labels is not None:
        # Find the boundary between func_test and sec_test
        func_test_indices = []
        sec_test_indices = []

        for i, test_name in enumerate(x_axis_labels):
            if test_name.startswith("func_test"):
                func_test_indices.append(i)
            elif test_name.startswith("sec_test"):
                sec_test_indices.append(i)

        # If we have both types of tests, add a divider
        if func_test_indices and sec_test_indices:
            # Find the last functional test index
            last_func_index = max(func_test_indices)
            # Find the first security test index
            first_sec_index = min(sec_test_indices)

            # Add vertical line between the two sections
            # The line should be exactly between the last func test and first sec test
            divider_x = (last_func_index + first_sec_index) / 2.0

            # Get the y-axis limits
            y_min, y_max = ax.get_ylim()

            # Add a thick vertical line
            ax.axvline(
                x=divider_x,
                ymin=y_min,
                ymax=y_max,
                color="black",
                linewidth=3,
                alpha=0.8,
            )

            # Add some spacing by adjusting the x-axis limits slightly
            x_min, x_max = ax.get_xlim()
            ax.set_xlim(x_min - 0.1, x_max + 0.1)

    plt.tight_layout()

    if highlight_x:
        if isinstance(highlight_x, str):
            highlight_x = [highlight_x]  # allow single string
        xticklabels = ax.get_xticklabels()
        for label in xticklabels:
            if label.get_text() in highlight_x:
                label.set_fontweight("bold")
                label.set_color("navy")
                label.set_size(label.get_size() * 1.1)  # make slightly larger

    if highlight_y:
        if isinstance(highlight_y, str):
            highlight_y = [highlight_y]  # allow single string
        yticklabels = ax.get_yticklabels()
        for label in yticklabels:
            if label.get_text() in highlight_y:
                label.set_fontweight("bold")
                label.set_color("navy")
                label.set_size(label.get_size() * 1.1)  # make slightly larger

    if iu:
        suffix = "iu"
    elif iw:
        suffix = "iw"
    elif iv:
        suffix = "iv"
    else:
        suffix = "it"

    png_path = results_png_path(scenario_folder_path, suffix, it)
    os.makedirs(os.path.dirname(png_path), exist_ok=True)
    plt.savefig(png_path)
    plt.close()


def deep_update(original: dict, updates: dict) -> None:
    """Recursively update a nested dictionary with new values."""
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(original.get(key), dict):
            deep_update(original[key], value)
        else:
            original[key] = value


def test_and_evaluate_baxbench(SCENARIO, model_list_test=None):
    """Run BaxBench tests and evaluation for a scenario.

    Args:
        SCENARIO: Scenario object to test
        model_list_test: Optional list of models to test with (default: uses config)

    Returns:
        Full evaluation results from BaxBench, keyed by test name -> "lang
        framework model" -> {"status": ..., "container_logs": ..., "test_logs": ...}
    """
    from tasks import TaskHandler

    tasks_list = build_tasks(SCENARIO, model_list=model_list_test)
    handler = TaskHandler(tasks_list, results_dir=RESULTS_DIR, max_concurrent_runs=None)
    handler.run_tests(
        samples=[0], timeout=300, num_ports=10000, min_port=12345, force=True
    )
    evaluated = handler.evaluate_results(samples=[0], ks=[1])

    full_results: dict[str, dict[str, dict[str, str]]] = {}
    for task, result in evaluated:
        if len(result.full_results) == 0:
            continue
        assert len(result.full_results) == 1, result.full_results
        key = f"{task.env.language} {task.env.framework} {task.model}"
        for test_name in result.full_results[0].keys():
            full_results.setdefault(test_name, {})[key] = result.full_results[0][
                test_name
            ]
    return full_results


def clean_code(code: str) -> str:
    """Format and clean Python code using isort and black."""
    # red = RedBaron(code)
    # # Remove all comments
    # for comment in red.find_all("CommentNode"):
    #     comment.parent.remove(comment)
    # code = red.dumps()
    code = isort.code(code)
    try:
        code = black.format_str(code, mode=black.FileMode())
    except black.parsing.InvalidInput as e:  # parse error
        logger.warning(
            f"Black could not parse code, returning unformatted: {e}\n{code}"
        )
        return code
    return code
