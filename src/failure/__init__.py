"""Common, dependency-light failure-record primitives.

Domain packages own their failure kinds and specialised evidence, while every
record implements the same serialisation and prompt-feedback contract here.
This keeps scenario builder independent from Kubernetes orchestration details
and lets K8s-bench records remain backwards-compatible.
"""

from .persist import persist_failure_record
from .record import FailureRecord, RetryTarget
from .text import failure_prompt_header, sanitize_test_log_tail, tail, trim

__all__ = [
    "FailureRecord",
    "RetryTarget",
    "failure_prompt_header",
    "persist_failure_record",
    "sanitize_test_log_tail",
    "tail",
    "trim",
]
