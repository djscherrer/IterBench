# Appendix icon assets

These assets are exported from the canonical diagram sources in
`DiagramCreation/src/components/` at revision `e834408` (`Update thesis diagram
sources`). The SVG files are the editable source copies; the PDF files are the
LaTeX-friendly exports used by `appendix/glossary.tex`. The Kubernetes mark is
kept as both PNG (the original diagram asset) and PDF.

| Appendix cue | Asset |
| --- | --- |
| LLM agent | `llm-agent` |
| Continuing conversation | `conversation` |
| Scenario idea / light bulb | `scenario-idea` |
| API contract | `scenario-api` |
| Functional-test suite | `functional-test` |
| Container image | `docker` |
| Deployment specification | `kubernetes` |
| Locust workload | `locust` |
| Feedback report | `feedback-report` (or the `locust-statistics` + `cluster-diagnostics` pair) |
| Failure record | `failure-record` |
| Locust statistics | `locust-statistics` |
| System diagnostics | `cluster-diagnostics` |

## Colored LaTeX variants

The `colored/` directory contains the palette-matched copies used by the
Diagram Notation table in Appendix A: scenario `#fbbf24`, functional tests
`#166534`, feedback and Locust statistics `#7c3aed`, diagnostics `#2563eb`,
failure `#dc2626`, and the neutral agent/conversation pair `#475569`.
Docker, Kubernetes, and Locust retain their source colors.
