"""Result-table rendering (pass@k / security-pass tables), as text, not figures."""

from .pass_at_k_table import (
    color_blue,
    color_cyan,
    color_func,
    color_sec,
    tasks_and_results_to_table,
    tasks_and_results_to_table_averages,
)

__all__ = [
    "color_blue",
    "color_cyan",
    "color_func",
    "color_sec",
    "tasks_and_results_to_table",
    "tasks_and_results_to_table_averages",
]
