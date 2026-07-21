"""Puts baxbench's top-level ``src/`` on ``sys.path``.

scenario_builder is executed with its own directory as ``sys.path[0]`` (via
``python main.py``), so baxbench's ``env``, ``scenarios``, ``llm``, ``cwes``
etc. aren't importable unless baxbench's ``src/`` is added too. Appended, not
inserted at 0, so scenario_builder's own local modules keep priority over any
same-named baxbench module — only names that don't exist locally fall through
to baxbench's.

Imported for its side effect (the ``sys.path.append`` below) at the top of
``config.py``, the earliest point every entry point passes through, so it has
already run by the time any other module tries to import something from
baxbench.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BAXBENCH_SRC = Path(__file__).resolve().parents[1]

if str(_BAXBENCH_SRC) not in sys.path:
    sys.path.append(str(_BAXBENCH_SRC))
