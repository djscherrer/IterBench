#!/usr/bin/env python3
"""
Generate the per-iteration results appendix (Appendix ``per_iteration_results``).

Emits one landscape ``longtable`` per model. Each table is grouped into seven
scenario bands; within a band there is one row per framework
(``Go-net-http`` / ``Python-Flask`` / ``Rust-Actix``) and one column per
iteration (baseline plus refinement steps 1--10). Every cell shows the
iteration's sustained-goodput outcome. Positive estimates after a refinement
are lightly marked by refinement stage.

Cell states:

- positive sustained-goodput estimate: the measured value in req/s (row
  maximum in bold);
- ``warm-up F.``: the warm-up health gate failed, so explore was not entered;
- ``recovery F.``: explore recorded a positive transient peak, but recovery
  failed before refine;
- ``no settle``: refine was entered, but accepted no stable level;
- a failure keyword (italic, shaded): the pipeline stage that failed, so no
  goodput was measured at all.

Sustained-goodput estimates and failure classifications come from
``results_aggregate/iterations.csv`` and ``results_aggregate/failures.csv``.
The no-estimate states come from ``results_aggregate/load_profile_phases.csv``
(the same tables the Chapter figures use). Refinement stages come from the
``refinement_kind`` field in ``results_aggregate/iterations.csv``.

Usage:
    python scripts/analysis/per_iteration_results.py
    # writes Writeup/appendix/per_iteration_results.tex
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Final dataset: three models, seven scenarios, three frameworks. Order matches
# the Chapter 6 results tables (opus, gpt-5.5, glm) and the setup chapter.
MODELS = [
    ("anthropic-claude-opus-4-8", "claude-opus-4-8"),
    ("openai-gpt-5.5-2026-04-23", "gpt-5.5"),
    ("z-ai-glm-5.2", "glm-5.2"),
]
MODEL_TEX = {
    "anthropic-claude-opus-4-8": r"\ClaudeModel{}",
    "openai-gpt-5.5-2026-04-23": r"\GPTModel{}",
    "z-ai-glm-5.2": r"\GLMModel{}",
}
SCENARIOS = [
    ("ClickCount", "ClickCount"),
    ("Recipes", "Recipes"),
    ("Petstore", "Petstore"),
    ("BranchWeave_InteractiveStoryGraph", "BranchWeave"),
    ("ParcelPinLockerPickup", "ParcelPinLockerPickup"),
    ("SplitNestSharedExpenseLedger", "SplitNestSharedExpenseLedger"),
    ("TransitPulseDelayReporter", "TransitPulseDelayReporter"),
]
FRAMEWORKS = [
    ("Go-net-http", "Go"),
    ("Python-Flask", "Flask"),
    ("Rust-Actix", "Actix"),
]
N_ITER = 11  # iteration 0 (baseline) .. 10

# Short failure labels for the grid; the legend maps them back to the harness
# classifier's names.
FAIL_SHORT = {
    "functional_test": "func",
    "docker_build": "build",
    "spec_validation": "spec",
    "llm_parse": "parse",
    "llm_call": "call",
    "crashloop": "crash",
    "timeout": "timeout",
    "unknown": "unknown",
}

NO_ESTIMATE_LABELS = {"warm-up F.", "recovery F.", "no settle"}


def _num(x: float) -> str:
    """Integer req/s with a LaTeX thousands separator, e.g. 14272 -> 14{,}272."""
    return f"{int(round(x)):,}".replace(",", "{,}")


def _load_goodput(path: Path) -> dict:
    out: dict = {}
    with path.open() as f:
        for r in csv.DictReader(f):
            key = (r["model"], r["scenario"], r["env"], int(r["iteration_index"]))
            out[key] = float(r["goodput_rps"]) if r["goodput_rps"] != "" else None
    return out


def _load_refinement_kinds(path: Path) -> dict:
    out: dict = {}
    with path.open() as f:
        for r in csv.DictReader(f):
            key = (r["model"], r["scenario"], r["env"], int(r["iteration_index"]))
            out[key] = r["refinement_kind"]
    return out


def _load_failures(path: Path) -> dict:
    out: dict = {}
    with path.open() as f:
        for r in csv.DictReader(f):
            key = (r["model"], r["scenario"], r["env"], int(r["iteration_index"]))
            out[key] = r["failure_kind"] or "unknown"
    return out


def _bool_field(value: str, *, field: str, key: tuple) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ValueError(f"invalid {field}={value!r} for {key}")


def _load_no_estimate_states(path: Path) -> dict:
    """Map completed benches without a sustained estimate to display states."""
    out: dict = {}
    with path.open() as f:
        for r in csv.DictReader(f):
            sustained = float(r["sustained_goodput_rps"] or 0.0)
            if sustained > 0:
                continue
            key = (r["model"], r["scenario"], r["env"], int(r["iteration_index"]))
            if not _bool_field(
                r["warmup_healthy"], field="warmup_healthy", key=key
            ):
                label = "warm-up F."
            elif not _bool_field(
                r["refine_attempted"], field="refine_attempted", key=key
            ):
                label = "recovery F."
            else:
                label = "no settle"
            out[key] = label
    return out


def _cell(model_dir, scen_dir, fw_dir, idx, goodput, refinement, no_estimate, failures):
    """Return (LaTeX cell body, goodput-or-None) for one iteration."""
    key = (model_dir, scen_dir, fw_dir, idx)
    gp = goodput.get(key)
    if gp is not None and gp > 0:
        fg = {"code": "codecol", "spec": "speccol"}.get(refinement.get(key))
        value = rf"\textcolor{{{fg}}}{{{_num(gp)}}}" if fg else _num(gp)
        return rf"\makecell{{{value}}}", gp
    if gp is not None:
        label = no_estimate.get(key)
        if label not in NO_ESTIMATE_LABELS:
            raise ValueError(f"missing no-estimate state for {key}")
        label_tex = rf"{{\fontsize{{7}}{{7}}\selectfont\itshape {label}}}"
        return rf"\makecell{{{label_tex}}}", None
    kind = FAIL_SHORT.get(failures.get(key, "unknown"), "unknown")
    body = rf"\cellcolor{{failbg}}\makecell{{{{\itshape\textcolor{{failcol}}{{{kind}}}}}}}"
    return body, None


def _model_table(model_dir, model_disp, goodput, refinement, no_estimate, failures) -> str:
    col_head = " & ".join([r"\textbf{Base}"] + [rf"\textbf{{{i}}}" for i in range(1, N_ITER)])
    # A manual caption (own counter + List-of-Tables entry) rather than a float:
    # a full-page rotated float inside pdflscape leaves stray blank pages. The
    # table is boxed first so the caption can be set in a \parbox of exactly the
    # table's width, keeping the two the same width and centred together instead
    # of letting the caption sprawl across the whole landscape page.
    display_tex = MODEL_TEX[model_dir]
    caption = (
        r"Per-iteration sustained-goodput outcomes (req/s; "
        r"\textbf{bold} marks each row's best) for "
        rf"\textbf{{{display_tex}}}. "
        rf"\textcolor{{codecol}}{{Code refinement}} is green; "
        r"\textcolor{speccol}{deployment-specification refinement} is blue. "
        r"State labels: p.~\pageref{ch:appendix-per-iteration}; "
        r"failures: Table~\ref{tab:appendix-iter-legend}."
    )
    lines = [
        r"\begingroup",
        rf"\refstepcounter{{table}}\label{{tab:appendix-iter-{model_disp}}}%",
        rf"\addcontentsline{{lot}}{{table}}{{\protect\numberline{{\thetable}}Per-iteration "
        rf"sustained-goodput outcomes, {display_tex}}}%",
        r"\centering",
        r"\scriptsize\setlength{\tabcolsep}{3pt}\renewcommand{\arraystretch}{1.35}",
        # makecell resets arraystretch to 1 unless its private setup is
        # overridden; the slightly roomier rows improve scanability.
        r"\renewcommand{\cellset}{\def\arraystretch{1.35}\setlength{\extrarowheight}{0pt}\nomakegapedcells}",
        r"\sbox0{\begin{tabular}{@{}l|*{11}{C}@{}}",
        r"\toprule",
        rf"\textbf{{Fw}} & {col_head} \\",
        r"\midrule",
    ]
    for si, (scen_dir, scen_disp) in enumerate(SCENARIOS):
        if si:
            lines.append(r"\midrule")
        lines.append(
            rf"\multicolumn{{12}}{{@{{}}l}}{{\cellcolor{{bandbg}}\textsc{{{scen_disp}}}}}\\"
        )
        for fw_dir, fw_disp in FRAMEWORKS:
            cells, vals = [], []
            for idx in range(N_ITER):
                tex, gp = _cell(
                    model_dir,
                    scen_dir,
                    fw_dir,
                    idx,
                    goodput,
                    refinement,
                    no_estimate,
                    failures,
                )
                cells.append(tex)
                vals.append(gp)
            row_max = max([v for v in vals if v is not None and v > 0], default=None)
            if row_max is not None:
                for i, gp in enumerate(vals):
                    if gp == row_max:
                        cells[i] = cells[i].replace(
                            r"\makecell{", r"\makecell{\bfseries ", 1
                        )
            lines.append(rf"\textsc{{{fw_disp}}} & " + " & ".join(cells) + r" \\")
    lines += [
        r"\bottomrule",
        r"\end{tabular}}%",
        r"\begin{minipage}{\wd0}",
        rf"\footnotesize Table~\thetable: {caption}\par",
        r"\vspace{2pt}",
        r"\usebox0",
        r"\end{minipage}\par",
        r"\endgroup",
    ]
    return "\n".join(lines)


PREAMBLE = r"""\label{ch:appendix-per-iteration}

\section{Detailed Per-Iteration Results}

This appendix lists the full per-iteration record behind
Chapter~\ref{ch:results}: every one of the $3\times7\times3=63$
model$\times$scenario$\times$framework tasks, and within each task the
sustained-goodput outcome of all eleven iterations (the baseline plus ten
refinement steps). The aggregate figures in
Chapter~\ref{ch:results} are computed from the numeric estimates recorded
here; the state labels preserve the remaining outcomes so the record can be
checked task by task.

\paragraph{How to read an entry.} There is one table per model, split into
seven scenario bands, with one row per framework and one column per
iteration (\textbf{Base} is iteration~000, the pre-refinement baseline).
The main item in an entry is either that iteration's sustained-goodput
estimate in req/s or a short state label. The highest sustained-goodput
estimate in each framework row is set in bold. For positive estimates after a
refinement, \textcolor{codecol}{code refinement} is shown in green and
\textcolor{speccol}{deployment-specification refinement} in blue; baseline
estimates are black. Detailed
LLM costs are reported separately in Tables~D.6--D.10. An entry can take five
forms:

\begin{itemize}
  \item a positive value: refine established that sustained-goodput estimate;
  \item \textit{warm-up F.}: the completed load test failed the warm-up health
    gate, so explore was not entered; this does not imply that no successful
    requests were served during warm-up;
  \item \textit{recovery F.}: explore recorded a positive transient peak, but
    recovery failed and refine was never entered;
  \item \textit{no settle}: refine was entered, but accepted no stable level;
  \item an italic, shaded keyword: the iteration failed at a pipeline
    stage before any goodput could be measured.
\end{itemize}

The state labels are not numeric zeros. In particular, the transient explore
peak is not substituted for a missing sustained estimate; it remains visible
in the RQ1 trajectory figure.

\begin{table}[htbp]
  \centering
  \caption{Failure keywords used in the per-iteration tables, as recorded
    by the pipeline's stage classifier. A keyword names the first stage
    that failed in that iteration.}
  \label{tab:appendix-iter-legend}
  \small
  \begin{tabular}{@{}ll@{}}
    \toprule
    \textbf{Keyword} & \textbf{Failed stage} \\
    \midrule
    \textit{build}   & Container image build (\texttt{docker\_build}) \\
    \textit{func}    & Functional test suite (\texttt{functional\_test}) \\
    \textit{spec}    & Deployment-spec validation (\texttt{spec\_validation}) \\
    \textit{crash}   & Pod crash loop after deploy (\texttt{crashloop}) \\
    \textit{timeout} & Deployment did not become ready in time (\texttt{timeout}) \\
    \textit{parse}   & Model output could not be parsed (\texttt{llm\_parse}) \\
    \textit{call}    & LLM API call failed (\texttt{llm\_call}) \\
    \textit{unknown} & Uncategorised failure (\texttt{unknown}) \\
    \bottomrule
  \end{tabular}
\end{table}

The abbreviated framework labels are \textsc{Go} (\textsc{Go-net-http}),
\textsc{Flask} (\textsc{Python-Flask}) and \textsc{Actix}
(\textsc{Rust-Actix}).

\FloatBarrier
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agg", type=Path, default=_REPO_ROOT / "results_aggregate")
    ap.add_argument(
        "--out",
        type=Path,
        default=_REPO_ROOT / "Writeup" / "appendix" / "per_iteration_results.tex",
    )
    args = ap.parse_args()

    goodput = _load_goodput(args.agg / "iterations.csv")
    refinement = _load_refinement_kinds(args.agg / "iterations.csv")
    no_estimate = _load_no_estimate_states(args.agg / "load_profile_phases.csv")
    failures = _load_failures(args.agg / "failures.csv")

    parts = [PREAMBLE]
    # Landscape geometry for these three pages only (stock layout is kept
    # elsewhere via geometry's [pass] and restored afterwards). When rotated,
    # \textwidth is the page's vertical extent: set it just above the table's
    # ~174mm width so the caption wraps to the table width instead of sprawling.
    # \textheight (the horizontal extent) is left generous; \vspace*{\fill} above
    # and below each table centres it, giving symmetric side margins that match
    # the body's (~42mm). All three share one landscape environment so no blank
    # page is left between them.
    parts.append(r"\newgeometry{hmargin=0.9cm,vmargin=1.4cm}")
    parts.append(r"\begin{landscape}\thispagestyle{plain}")
    for i, (model_dir, model_disp) in enumerate(MODELS):
        if i:
            parts.append(r"\newpage")
        parts.append(
            _model_table(model_dir, model_disp, goodput, refinement, no_estimate, failures)
        )
    parts.append(r"\end{landscape}")
    parts.append(r"\restoregeometry")

    args.out.write_text("\n".join(parts) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
