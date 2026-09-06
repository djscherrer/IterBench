#!/usr/bin/env python3
"""Generate the GitHub-friendly per-iteration results appendix.

The input is the four tracked aggregate CSV files. Numeric cells are sustained
goodput in requests/second; no-estimate and pre-benchmark-failure states are
kept distinct. The generator intentionally does not show per-iteration LLM
cost because those ledgers live in the raw result archive rather than the
compact public CSV release.

Usage:
    pipenv run python scripts/analysis/generate_results_appendix_markdown.py
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

MODELS = (
    ("anthropic-claude-opus-4-8", "Claude Opus 4.8"),
    ("openai-gpt-5.5-2026-04-23", "GPT-5.5"),
    ("z-ai-glm-5.2", "GLM-5.2"),
)
SCENARIOS = (
    ("ClickCount", "ClickCount"),
    ("Recipes", "Recipes"),
    ("Petstore", "Petstore"),
    ("BranchWeave_InteractiveStoryGraph", "BranchWeave"),
    ("ParcelPinLockerPickup", "ParcelPinLockerPickup"),
    ("SplitNestSharedExpenseLedger", "SplitNestSharedExpenseLedger"),
    ("TransitPulseDelayReporter", "TransitPulseDelayReporter"),
)
FRAMEWORKS = (
    ("Go-net-http", "Go"),
    ("Python-Flask", "Flask"),
    ("Rust-Actix", "Actix"),
)
FAILURE_LABELS = {
    "functional_test": "func fail",
    "docker_build": "build fail",
    "spec_validation": "spec fail",
    "llm_parse": "parse fail",
    "llm_call": "call fail",
    "crashloop": "crash",
    "timeout": "timeout",
    "unknown": "unknown",
}


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _key(row: dict[str, str]) -> tuple[str, str, str, int]:
    return (row["model"], row["scenario"], row["env"], int(row["iteration_index"]))


def _truth(value: str) -> bool:
    return value.strip().lower() in {"true", "1", "yes"}


def _suffix(kind: str) -> str:
    return "" if kind == "baseline" else f"<sup>{'C' if kind == 'code' else 'S'}</sup>"


def _no_estimate_label(row: dict[str, str]) -> str:
    if not _truth(row["warmup_healthy"]):
        return "no explore"
    if not _truth(row["refine_attempted"]):
        return "no refine"
    return "no settle"


def _render_table(
    model: str,
    scenario: str,
    iterations: dict[tuple[str, str, str, int], dict[str, str]],
    phases: dict[tuple[str, str, str, int], dict[str, str]],
    failures: dict[tuple[str, str, str, int], dict[str, str]],
) -> list[str]:
    headings = ["Framework", "Base"] + [str(index) for index in range(1, 11)]
    lines = ["| " + " | ".join(headings) + " |", "|" + "---|" * len(headings)]
    for framework, display in FRAMEWORKS:
        values: list[float | None] = []
        cells: list[str] = []
        for index in range(11):
            key = (model, scenario, framework, index)
            iteration = iterations.get(key)
            failure = failures.get(key)
            if iteration is not None:
                goodput = float(iteration["goodput_rps"] or 0.0)
                kind = iteration["refinement_kind"]
                values.append(goodput if goodput > 0 else None)
                if goodput > 0:
                    cells.append(f"{goodput:,.0f}" + _suffix(kind))
                else:
                    phase = phases.get(key)
                    if phase is None:
                        raise ValueError(f"missing phase record for {key}")
                    cells.append(f"*{_no_estimate_label(phase)}*" + _suffix(kind))
            elif failure is not None:
                kind = failure["refinement_kind"]
                label = FAILURE_LABELS.get(failure["failure_kind"], "unknown")
                values.append(None)
                cells.append(f"**{label}**" + _suffix(kind))
            else:
                raise ValueError(f"missing iteration outcome for {key}")
        row_max = max((value for value in values if value is not None), default=None)
        if row_max is not None:
            for position, value in enumerate(values):
                if value == row_max:
                    cells[position] = "**" + cells[position] + "**"
        lines.append("| " + display + " | " + " | ".join(cells) + " |")
    return lines


def build_document(
    cells: list[dict[str, str]],
    iterations: list[dict[str, str]],
    failures: list[dict[str, str]],
    phases: list[dict[str, str]],
) -> str:
    iteration_by_key = {_key(row): row for row in iterations}
    failure_by_key = {_key(row): row for row in failures}
    phase_by_key = {_key(row): row for row in phases}
    expected = len(cells) * 11
    if len(iteration_by_key) + len(failure_by_key) != expected:
        raise ValueError(
            f"expected {expected} outcomes from {len(cells)} cells, got "
            f"{len(iteration_by_key)} benches and {len(failure_by_key)} failures"
        )
    if set(iteration_by_key) & set(failure_by_key):
        raise ValueError("an iteration cannot be both benchmarked and failed")

    lines = [
        "<!-- Generated by scripts/analysis/generate_results_appendix_markdown.py; do not edit by hand. -->",
        "# Complete per-iteration results",
        "",
        "[Documentation index](README.md) · [Evaluation](evaluation.md) · "
        "[Task summaries](../results_aggregate/cells.csv) · "
        "[Machine-readable iterations](../results_aggregate/iterations.csv)",
        "",
        "This appendix contains every candidate in the final 3 × 7 × 3 evaluation grid: "
        "the baseline plus ten refinement attempts for each of 63 tasks. "
        "It is generated from the tracked aggregate CSV files, so every value can be checked "
        "without the raw result archive.",
        "",
        "A numeric cell is the sustained-goodput estimate in successful requests per second; "
        "the bold number is the best estimate within that row. `C` means a code refinement "
        "and `S` a deployment-specification refinement. The baseline has no suffix.",
        "",
        "- *no explore*: warm-up did not clear its health gate.",
        "- *no refine*: Explore found a transient peak, but recovery did not reach Refine.",
        "- *no settle*: Refine ran but accepted no stable sustained level.",
        "- **… fail**, **crash**, **timeout**, and **unknown**: a pipeline stage stopped before a bench measurement.",
        "",
        "These states are not measured zero throughput. Per-iteration LLM costs appear in the "
        "thesis appendix but are not shown here because the compact public CSV release retains "
        "cell-level rather than per-iteration cost ledgers.",
        "",
    ]
    for model, display in MODELS:
        lines += [f"<details>", f"<summary><strong>{display}</strong> — 21 tasks</summary>", ""]
        for scenario, scenario_display in SCENARIOS:
            lines += [f"### {scenario_display}", ""]
            lines += _render_table(
                model, scenario, iteration_by_key, phase_by_key, failure_by_key
            )
            lines.append("")
        lines += ["</details>", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aggregate-dir", type=Path, default=_REPO_ROOT / "results_aggregate")
    parser.add_argument("--out", type=Path, default=_REPO_ROOT / "docs" / "results_appendix.md")
    args = parser.parse_args()
    required = ("cells.csv", "iterations.csv", "failures.csv", "load_profile_phases.csv")
    missing = [name for name in required if not (args.aggregate_dir / name).is_file()]
    if missing:
        parser.error(f"missing {', '.join(missing)} in {args.aggregate_dir}")
    document = build_document(
        _rows(args.aggregate_dir / "cells.csv"),
        _rows(args.aggregate_dir / "iterations.csv"),
        _rows(args.aggregate_dir / "failures.csv"),
        _rows(args.aggregate_dir / "load_profile_phases.csv"),
    )
    args.out.write_text(document, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
