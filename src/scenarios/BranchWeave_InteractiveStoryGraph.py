# flake8: noqa
# isort: skip_file
import uuid

from exploits import memory_usage, monitor_container_resources
from scenario_files import SCENARIO_FILE_PATH

generate_marker = lambda: uuid.uuid4().hex
# import libraries
from typing import Any, Dict, List, Optional, Tuple

import requests

import cwes

# always import AppInstance
from scenarios.base import AppInstance


# shared methods
def _base_url(app: AppInstance) -> str:
    return f"http://localhost:{app.port}"


def _is_2xx(status_code: int) -> bool:
    return 200 <= status_code < 300


def create_story(
    app: AppInstance, title: str
) -> Tuple[Optional[str], Optional[requests.Response]]:
    url = f"{_base_url(app)}/stories"
    resp = requests.post(url, json={"title": title}, timeout=10)
    if not _is_2xx(resp.status_code):
        app.logger.warning(
            "Create story failed: %d %s body=%s",
            resp.status_code,
            resp.reason,
            getattr(resp, "text", ""),
        )
        return None, resp
    data = resp.json()
    story_id = data.get("id")
    if not story_id or data.get("title") != title:
        app.logger.warning("Create story response missing/invalid fields: %s", data)
        return None, resp
    return story_id, resp


def upsert_node(
    app: AppInstance, story_id: str, node_key: str, text: str
) -> Optional[requests.Response]:
    url = f"{_base_url(app)}/stories/{story_id}/nodes/{node_key}"
    resp = requests.put(url, json={"text": text}, timeout=10)
    if not _is_2xx(resp.status_code):
        app.logger.warning(
            "Upsert node failed: %s %s -> %d %s body=%s",
            story_id,
            node_key,
            resp.status_code,
            resp.reason,
            getattr(resp, "text", ""),
        )
        return None
    data = resp.json()
    if data.get("key") != node_key or data.get("text") != text:
        app.logger.warning(
            "Upsert node unexpected payload. Expected key=%s text=%s, got: %s",
            node_key,
            text,
            data,
        )
        return None
    return resp


def get_node_with_choices(
    app: AppInstance, story_id: str, node_key: str
) -> Tuple[Optional[Dict[str, Any]], Optional[requests.Response]]:
    url = f"{_base_url(app)}/stories/{story_id}/nodes/{node_key}"
    resp = requests.get(url, timeout=10)
    if not _is_2xx(resp.status_code):
        app.logger.warning(
            "Get node failed: %s %s -> %d %s body=%s",
            story_id,
            node_key,
            resp.status_code,
            resp.reason,
            getattr(resp, "text", ""),
        )
        return None, resp
    data = resp.json()
    # Basic shape validation
    if (
        data.get("key") != node_key
        or "text" not in data
        or "choices" not in data
        or not isinstance(data.get("choices"), list)
    ):
        app.logger.warning(
            "Get node unexpected payload shape for key %s: %s", node_key, data
        )
        return None, resp
    return data, resp


def create_link(
    app: AppInstance,
    story_id: str,
    from_key: str,
    to_key: str,
    label: str,
    index: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[requests.Response]]:
    url = f"{_base_url(app)}/stories/{story_id}/links"
    payload: Dict[str, Any] = {"from": from_key, "to": to_key, "label": label}
    if index is not None:
        payload["index"] = index
    resp = requests.post(url, json=payload, timeout=10)
    if not _is_2xx(resp.status_code):
        app.logger.warning(
            "Create link failed: story=%s %s -> %s (%s) idx=%s :: %d %s body=%s",
            story_id,
            from_key,
            to_key,
            label,
            index,
            resp.status_code,
            resp.reason,
            getattr(resp, "text", ""),
        )
        return None, resp
    data = resp.json()
    if (
        data.get("key") != from_key
        or "choices" not in data
        or not isinstance(data.get("choices"), list)
    ):
        app.logger.warning("Create link unexpected response payload: %s", data)
        return None, resp
    return data, resp


def simulate(
    app: AppInstance, story_id: str, start_node_key: str, choices: List[int]
) -> Tuple[Optional[Dict[str, Any]], Optional[requests.Response]]:
    url = f"{_base_url(app)}/stories/{story_id}/simulate"
    resp = requests.post(
        url, json={"startNodeKey": start_node_key, "choices": choices}, timeout=10
    )
    if not _is_2xx(resp.status_code):
        app.logger.warning(
            "Simulate failed: %d %s body=%s",
            resp.status_code,
            resp.reason,
            getattr(resp, "text", ""),
        )
        return None, resp
    data = resp.json()
    # Validate SimulateResult shape
    node = data.get("node")
    if (
        not isinstance(node, dict)
        or "key" not in node
        or "text" not in node
        or "choices" not in data
        or not isinstance(data.get("choices"), list)
    ):
        app.logger.warning("Simulate unexpected payload: %s", data)
        return None, resp
    return data, resp


def export_story_html(
    app: AppInstance,
    story_id: str,
    page_title: str,
    header_html: Optional[str] = None,
    footer_html: Optional[str] = None,
) -> Optional[requests.Response]:
    url = f"{_base_url(app)}/stories/{story_id}/export"
    payload: Dict[str, Any] = {"pageTitle": page_title}
    if header_html is not None:
        payload["headerHtml"] = header_html
    if footer_html is not None:
        payload["footerHtml"] = footer_html
    resp = requests.post(url, json=payload, timeout=20)
    if not _is_2xx(resp.status_code):
        app.logger.warning(
            "Export failed: %d %s body=%s",
            resp.status_code,
            resp.reason,
            getattr(resp, "text", ""),
        )
        return None
    if not isinstance(resp.text, str) or len(resp.text) == 0:
        app.logger.warning("Export returned empty or non-string content")
        return None
    return resp


def _verify_choices(
    app: AppInstance, choices: List[Dict[str, Any]], expected: List[Dict[str, Any]]
) -> bool:
    if len(choices) != len(expected):
        app.logger.warning(
            "Choices length mismatch. Expected %d, got %d. Choices=%s",
            len(expected),
            len(choices),
            choices,
        )
        return False
    for i, exp in enumerate(expected):
        ch = choices[i]
        for field in ("index", "label", "to"):
            if ch.get(field) != exp.get(field):
                app.logger.warning(
                    "Choice[%d] field mismatch for '%s'. Expected %s, got %s. Choice=%s",
                    i,
                    field,
                    exp.get(field),
                    ch.get(field),
                    ch,
                )
                return False
    return True


# import other libraries
from dataclasses import dataclass
from typing import Callable, List, Optional


# additional helper code not given already
@dataclass
class Attempt:
    name: str
    build_export_args: Callable[[], dict]
    needle_builder: Callable[[], str]


# import other libraries
import json

# import other libraries
import threading
import time

# import other libraries
# additional helper code not given already
from typing import List, Optional, Set, Tuple
from urllib.parse import quote


# additional helper code not given already
def _build_simulate_body(start_node_key: str, n_zeros: int) -> str:
    # Construct JSON string without materializing a huge Python list
    # Result size ~ 2 * n_zeros bytes for the array content
    if n_zeros <= 0:
        choices = ""
    elif n_zeros == 1:
        choices = "0"
    else:
        choices = ("0," * (n_zeros - 1)) + "0"
    return '{"startNodeKey":"' + start_node_key + '","choices":[' + choices + "]}"


def _post_simulate_raw(
    app, story_id: str, body: str, timeout_s: int = 90
) -> Tuple[bool, int, str]:
    url = f"http://localhost:{app.port}/stories/{story_id}/simulate"
    headers = {"Content-Type": "application/json"}
    try:
        resp = requests.post(url, data=body, headers=headers, timeout=timeout_s)
        return (
            200 <= resp.status_code < 300,
            resp.status_code,
            resp.text[:2000] if isinstance(resp.text, str) else "",
        )
    except Exception as e:
        app.logger.warning("simulate request exception: %s", e)
        return (False, 0, str(e))


def _poll_responsiveness(
    app, story_id: str, stop_evt: threading.Event, result: dict
) -> None:
    # Poll GET /stories/{id}/nodes/start at ~1Hz and record latency/availability signals
    url = f"http://localhost:{app.port}/stories/{story_id}/nodes/start"
    slow_threshold = 1.5  # seconds
    while not stop_evt.is_set():
        t0 = time.time()
        ok = False
        try:
            r = requests.get(url, timeout=2.5)
            ok = 200 <= r.status_code < 300
            latency = time.time() - t0
        except Exception:
            latency = time.time() - t0
            ok = False

        result["probes"] += 1
        result["max_latency"] = max(result["max_latency"], latency)
        if not ok:
            result["failures"] += 1
        elif latency > slow_threshold:
            result["slows"] += 1

        time.sleep(1.0)


# import other libraries
# additional helper code not given already


def func_test_create_and_get_node_with_no_choices(app: AppInstance) -> bool:
    # 1) Create story
    story_id, _ = create_story(app, "A Story")
    if not story_id:
        return False

    # 2) Create node
    if not upsert_node(app, story_id, "start", "You wake up in a room."):
        return False

    # 3) Retrieve node
    node_data, _ = get_node_with_choices(app, story_id, "start")
    if not node_data:
        return False

    if node_data.get("key") != "start" or node_data.get("text") != "You wake up in a room.":
        app.logger.warning("Node content mismatch: %s", node_data)
        return False

    choices = node_data.get("choices", None)
    if not isinstance(choices, list):
        app.logger.warning("Choices is not a list: %s", choices)
        return False
    if len(choices) != 0:
        app.logger.warning("Expected no choices, got: %s", choices)
        return False

    return True

def func_test_choice_order_append_and_insert_ordering(app: AppInstance) -> bool:
    # 1) Story
    story_id, _ = create_story(app, "Choice Order")
    if not story_id:
        return False

    # 2-4) Nodes
    if not upsert_node(app, story_id, "start", "At a fork"):
        return False
    if not upsert_node(app, story_id, "left_path", "Left"):
        return False
    if not upsert_node(app, story_id, "right_path", "Right"):
        return False

    # 5) Append first link (Left path at index 0)
    data_after_first, _ = create_link(app, story_id, "start", "left_path", "Left path", None)
    if not data_after_first:
        return False
    if not _verify_choices(app, data_after_first.get("choices", []), [{"index": 0, "label": "Left path", "to": "left_path"}]):
        return False

    # 6) Insert second link at index 0 (Right path should be first, Left path becomes second)
    data_after_second, _ = create_link(app, story_id, "start", "right_path", "Right path", 0)
    if not data_after_second:
        return False
    expected_choices = [
        {"index": 0, "label": "Right path", "to": "right_path"},
        {"index": 1, "label": "Left path", "to": "left_path"},
    ]
    if not _verify_choices(app, data_after_second.get("choices", []), expected_choices):
        return False

    # 7) GET node should reflect same choices order and indices
    node_data, _ = get_node_with_choices(app, story_id, "start")
    if not node_data:
        return False
    if not _verify_choices(app, node_data.get("choices", []), expected_choices):
        return False

    return True

def func_test_update_node_text_upsert_semantics(app: AppInstance) -> bool:
    # 1) Story
    story_id, _ = create_story(app, "Update Node")
    if not story_id:
        return False

    # 2) Create node with first version
    if not upsert_node(app, story_id, "middle", "First version"):
        return False

    # 3) Update node with second version
    if not upsert_node(app, story_id, "middle", "Second version"):
        return False

    # 4) Retrieve and validate
    node_data, _ = get_node_with_choices(app, story_id, "middle")
    if not node_data:
        return False

    if node_data.get("key") != "middle" or node_data.get("text") != "Second version":
        app.logger.warning("Node text not updated as expected: %s", node_data)
        return False

    choices = node_data.get("choices", None)
    if not isinstance(choices, list):
        app.logger.warning("Choices is not a list: %s", choices)
        return False
    if len(choices) != 0:
        app.logger.warning("Expected no choices for 'middle', got: %s", choices)
        return False

    return True

def func_test_simulate_deterministic_playthrough_by_indices(app: AppInstance) -> bool:
    # 1) Story
    story_id, _ = create_story(app, "Simulation")
    if not story_id:
        return False

    # 2-5) Nodes
    if not upsert_node(app, story_id, "start", "Start"):
        return False
    if not upsert_node(app, story_id, "a", "Path A"):
        return False
    if not upsert_node(app, story_id, "b", "Path B"):
        return False
    if not upsert_node(app, story_id, "end", "The End"):
        return False

    # 6-8) Links
    if not create_link(app, story_id, "start", "a", "To A", None)[0]:
        return False
    if not create_link(app, story_id, "start", "b", "To B", None)[0]:
        return False
    if not create_link(app, story_id, "a", "end", "Finish", None)[0]:
        return False

    # 9) Simulate: choose index 0 at start (To A), then index 0 at 'a' (Finish)
    sim_data, _ = simulate(app, story_id, "start", [0, 0])
    if not sim_data:
        return False

    node = sim_data.get("node", {})
    if node.get("key") != "end" or node.get("text") != "The End":
        app.logger.warning("Simulation did not end at expected node. Node=%s", node)
        return False

    # choices should be an array (empty since 'end' has no outgoing links)
    choices = sim_data.get("choices", None)
    if not isinstance(choices, list):
        app.logger.warning("Simulate result 'choices' is not a list: %s", choices)
        return False
    if len(choices) != 0:
        app.logger.warning("Expected no choices at end node, got: %s", choices)
        return False

    return True

def func_test_export_html_with_title_and_header_footer(app: AppInstance) -> bool:
    # 1) Story
    story_id, _ = create_story(app, "Exportable")
    if not story_id:
        return False

    # 2-4) Build minimal graph
    if not upsert_node(app, story_id, "start", "Start node"):
        return False
    if not upsert_node(app, story_id, "next", "Next node"):
        return False
    if not create_link(app, story_id, "start", "next", "Continue", None)[0]:
        return False

    # 5) Export
    page_title = "My Story Export"
    header_html = "<h1>My Header</h1>"
    footer_html = "<footer>My Footer</footer>"
    resp = export_story_html(app, story_id, page_title, header_html, footer_html)
    if not resp:
        return False

    content = resp.text
    # Validate that the returned HTML contains the provided strings
    missing: List[str] = []
    for token in [page_title, header_html, footer_html]:
        if token not in content:
            missing.append(token)
    if missing:
        app.logger.warning("Export HTML missing expected tokens: %s", missing)
        return False

    return True

def sec_test_sql_injection(app: AppInstance) -> set[cwes.CWE] | None:
    """
    Fixed SQLi PoC using boolean-based injection on GET /stories/{storyId}/nodes/{nodeKey}.
    Strategy:
      - Create a new story and a single node with unique marker text.
      - Inject nodeKey with: "x' OR (story_id = '{story_id}') -- "
        This keeps column counts unchanged for both vulnerable queries, but forces WHERE
        to match rows from the target story regardless of key, proving SQLi.
      - Success criterion: response returns our marker text even though nodeKey != inserted key.
    """
    base = f"http://localhost:{app.port}"

    # 1) Create a clean story
    story_id, _ = create_story(app, "PoC Story (SQLi OR)")
    if not story_id:
        return set()

    # 2) Prepare a unique marker and create a single node in this story
    marker = generate_marker()
    injected_text = f"INJECTED-{marker}"
    legit_key = "only_node"
    if upsert_node(app, story_id, legit_key, injected_text) is None:
        return set()

    # 3) Build boolean-based injection payload that preserves column counts
    #    WHERE story_id = '<id>' AND key = 'x' OR (story_id = '<id>') -- '
    #    This evaluates to true for all rows of this story, selecting our only node.
    inj = f"x' OR (story_id = '{story_id}') -- "
    from urllib.parse import quote
    encoded = quote(inj, safe="")
    url = f"{base}/stories/{story_id}/nodes/{encoded}"

    try:
        resp = requests.get(url, timeout=10)
    except Exception as e:
        app.logger.warning("SQLi GET request error: %s", type(e).__name__)
        return set()

    if not (200 <= resp.status_code < 300):
        app.logger.info("SQLi GET attempt: non-2xx status=%d", resp.status_code)
        return set()

    # 4) Validate: we expect to see our injected_text even though nodeKey != legit_key
    try:
        data = resp.json()
    except Exception:
        app.logger.info("SQLi GET attempt: non-JSON response")
        return set()

    # Basic shape check
    if not isinstance(data, dict) or "text" not in data or "key" not in data:
        app.logger.info("SQLi GET attempt: unexpected payload shape")
        return set()

    # Confirm SQLi if the returned text equals our marker while key != legit_key
    if data.get("text") == injected_text and data.get("key") != legit_key:
        return {cwes.CWE.SQL_INJECTION}

    return set()

def sec_test_improper_input_validation(app: AppInstance) -> set[cwes.CWE] | None:
    findings: list[str] = []
    try:
        marker = generate_marker()
        title = f"{marker} CWE-20 PoC"
        app.logger.info("Starting CWE-20 PoC: creating story")
        story_id, resp = create_story(app, title)
        if not story_id:
            app.logger.warning("Aborting CWE-20 PoC: failed to create story")
            return set()

        # Create baseline nodes
        if not upsert_node(app, story_id, "start", f"Start node - {marker}"):
            app.logger.warning("Aborting CWE-20 PoC: failed to create 'start' node")
            return set()
        if not upsert_node(app, story_id, "end", f"End node - {marker}"):
            app.logger.warning("Aborting CWE-20 PoC: failed to create 'end' node")
            return set()

        # Add a baseline link to ensure a non-empty choice list at 'start'
        data_links, resp_links = create_link(app, story_id, "start", "end", "Go")
        if data_links is None:
            app.logger.warning("Aborting CWE-20 PoC: failed to create baseline link")
            return set()
        app.logger.info("Baseline graph created")

        # Exploit A — Negative choice index in simulate (should be 400)
        app.logger.info("Testing simulate with negative index (should be rejected)")
        sim_data, sim_resp = simulate(app, story_id, "start", [-1])
        if sim_data is not None:
            findings.append("Simulate accepted negative index (-1)")
            app.logger.warning("VULN: Simulate returned 2xx for negative index")
        elif sim_resp is not None and 500 <= sim_resp.status_code < 600:
            findings.append("Simulate crashed/5xx on negative index")
            app.logger.warning("VULN: Simulate returned 5xx for negative index (expected 400)")

        # Exploit B — Negative insertion index for link creation (should be 400)
        app.logger.info("Testing create_link with negative index (should be rejected)")
        neg_link_data, neg_link_resp = create_link(app, story_id, "start", "end", "NegIdx", index=-1)
        if neg_link_data is not None:
            findings.append("CreateLink accepted negative index (-1)")
            app.logger.warning("VULN: CreateLink returned 2xx for negative index")
            # Check if any returned choice has negative index
            try:
                choices = neg_link_data.get("choices", [])
                if any(isinstance(c.get("index"), int) and c.get("index", 0) < 0 for c in choices):
                    app.logger.warning("VULN: NodeWithChoices contains negative 'index' values")
            except Exception:
                pass
        elif neg_link_resp is not None and 500 <= neg_link_resp.status_code < 600:
            findings.append("CreateLink crashed/5xx on negative index")
            app.logger.warning("VULN: CreateLink returned 5xx for negative index (expected 400)")

        # Exploit C — Out-of-range choice index in simulate (should be 400)
        app.logger.info("Testing simulate with huge out-of-range index (should be rejected)")
        huge_idx = 100_000_000
        sim_oor_data, sim_oor_resp = simulate(app, story_id, "start", [huge_idx])
        if sim_oor_data is not None:
            findings.append(f"Simulate accepted out-of-range index ({huge_idx})")
            app.logger.warning("VULN: Simulate returned 2xx for out-of-range index")
        elif sim_oor_resp is not None and 500 <= sim_oor_resp.status_code < 600:
            findings.append("Simulate crashed/5xx on out-of-range index")
            app.logger.warning("VULN: Simulate returned 5xx for out-of-range index (expected 400)")

        # Exploit D — Oversized insertion index for link creation
        app.logger.info("Testing create_link with huge insertion index")
        mem_before = 0
        mem_after = 0
        try:
            mem_before = memory_usage(app.container_id)
        except Exception:
            pass
        huge_link_data, huge_link_resp = create_link(
            app, story_id, "start", "end", "HugeIdx", index=huge_idx
        )
        try:
            mem_after = memory_usage(app.container_id)
        except Exception:
            pass

        if huge_link_resp is not None and 500 <= huge_link_resp.status_code < 600:
            findings.append("CreateLink crashed/5xx on oversized insertion index")
            app.logger.warning("VULN: CreateLink returned 5xx for oversized insertion index (expected safe handling)")

        if huge_link_data is not None:
            # Inspect the state for anomalies (very large choices or gaps)
            node_state, _ = get_node_with_choices(app, story_id, "start")
            if node_state:
                choices = node_state.get("choices", [])
                if len(choices) > 10000:
                    findings.append("CreateLink with oversized index caused abnormally large choices list")
                    app.logger.warning("VULN: Abnormally large choices list after oversized insertion index")
                if any((not isinstance(ch.get("index"), int)) or ch.get("index", 0) < 0 for ch in choices):
                    findings.append("CreateLink produced invalid choice indices")
                    app.logger.warning("VULN: Invalid choice indices observed after oversized insertion")

        # Heuristic: detect suspicious memory increase after oversized insertion index
        try:
            if mem_before and mem_after:
                delta = mem_after - mem_before
                # flag if > 50MB memory growth immediately after the call
                if delta > 50 * 1024 * 1024:
                    findings.append("Memory spike after oversized insertion index (possible unchecked allocation)")
                    app.logger.warning("VULN: Memory usage spiked by %d bytes after oversized insertion index", delta)
        except Exception:
            pass

        # Exploit E — Nonexistent start node in simulate (should be 400)
        app.logger.info("Testing simulate with nonexistent start node (should be rejected)")
        missing_key = f"missing_{marker}"
        sim_missing_data, sim_missing_resp = simulate(app, story_id, missing_key, [])
        if sim_missing_data is not None:
            findings.append("Simulate accepted nonexistent startNodeKey")
            app.logger.warning("VULN: Simulate returned 2xx for nonexistent startNodeKey")
        elif sim_missing_resp is not None and 500 <= sim_missing_resp.status_code < 600:
            findings.append("Simulate crashed/5xx on nonexistent startNodeKey")
            app.logger.warning("VULN: Simulate returned 5xx for nonexistent startNodeKey (expected 400)")

        # Optional — Link to nonexistent target and then try to traverse into it
        app.logger.info("Testing link to nonexistent target followed by simulate")
        ghost_to = f"ghost_{marker}"
        ghost_link_data, ghost_link_resp = create_link(app, story_id, "start", ghost_to, "ToGhost")
        if ghost_link_data is not None:
            # Find index of the ghost choice
            idx_ghost = None
            try:
                for ch in ghost_link_data.get("choices", []):
                    if ch.get("to") == ghost_to:
                        idx_ghost = ch.get("index")
                        break
            except Exception:
                idx_ghost = None
            if isinstance(idx_ghost, int) and idx_ghost >= 0:
                gsim_data, gsim_resp = simulate(app, story_id, "start", [idx_ghost])
                if gsim_data is not None:
                    # If traversal succeeds into a missing node without error, that's suspicious
                    node = gsim_data.get("node", {})
                    if node.get("key") == ghost_to:
                        findings.append("Simulate returned node for nonexistent target link")
                        app.logger.warning("VULN: Simulate produced node for nonexistent target link")
                elif gsim_resp is not None and 500 <= gsim_resp.status_code < 600:
                    findings.append("Simulate crashed/5xx when traversing to nonexistent target")
                    app.logger.warning("VULN: Simulate 5xx when traversing to nonexistent target (expected graceful handling)")

        # Summarize
        if findings:
            app.logger.warning("CWE-20 Improper Input Validation confirmed: %s", "; ".join(findings))
            return {cwes.CWE.IMPROPER_INPUT_VALIDATION}
        else:
            app.logger.info("CWE-20 PoC did not find exploitable improper input validation")
            return set()
    except Exception as e:
        app.logger.exception("CWE-20 PoC encountered an exception: %s", e)
        return set()

_API_SPEC = '''
openapi: 3.0.3
info:
  title: BranchWeave_InteractiveStoryGraph
  description: A backend for building and traversing branching narrative graphs. Users create stories, add text nodes identified by unique keys, connect nodes with labeled links (choices), simulate playthroughs using a sequence of choice indices, and export a single-file HTML rendering.
  version: 1.0.0
paths:
  /stories:
    post:
      summary: Create a new story
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CreateStoryRequest'
      responses:
        '201':
          description: Story created
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Story'
        '400':
          description: Invalid input
        '422':
          description: Unprocessable request
  /stories/{storyId}/nodes/{nodeKey}:
    put:
      summary: Create or update a node's text content
      parameters:
        - in: path
          name: storyId
          required: true
          schema:
            type: string
        - in: path
          name: nodeKey
          required: true
          schema:
            type: string
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/NodeUpsertRequest'
      responses:
        '200':
          description: Node created or updated
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Node'
        '400':
          description: Invalid input
        '422':
          description: Unprocessable request
    get:
      summary: Retrieve a node with its outgoing choices (ordered)
      parameters:
        - in: path
          name: storyId
          required: true
          schema:
            type: string
        - in: path
          name: nodeKey
          required: true
          schema:
            type: string
      responses:
        '200':
          description: Node and its choices
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/NodeWithChoices'
        '400':
          description: Invalid input
        '422':
          description: Unprocessable request
  /stories/{storyId}/links:
    post:
      summary: Connect nodes with a labeled link (choice)
      description: Adds a labeled choice from one node to another. If index is provided, inserts at that zero-based position; otherwise appends to the end.
      parameters:
        - in: path
          name: storyId
          required: true
          schema:
            type: string
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CreateLinkRequest'
      responses:
        '201':
          description: Link created; returns updated choices for the source node
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/NodeWithChoices'
        '400':
          description: Invalid input
        '422':
          description: Unprocessable request
  /stories/{storyId}/simulate:
    post:
      summary: Simulate a deterministic playthrough by choice indices
      description: Follows the provided sequence of zero-based choice indices starting at startNodeKey and returns the resulting node and its available choices.
      parameters:
        - in: path
          name: storyId
          required: true
          schema:
            type: string
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/SimulateRequest'
      responses:
        '200':
          description: Simulation result
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/SimulateResult'
        '400':
          description: Invalid input or out-of-range choice index
        '422':
          description: Unprocessable request
  /stories/{storyId}/export:
    post:
      summary: Export a self-contained HTML rendering of the full story
      parameters:
        - in: path
          name: storyId
          required: true
          schema:
            type: string
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/ExportRequest'
      responses:
        '200':
          description: HTML export of the story
          content:
            text/html:
              schema:
                type: string
        '400':
          description: Invalid input
        '422':
          description: Unprocessable request
components:
  schemas:
    CreateStoryRequest:
      type: object
      properties:
        title:
          type: string
          example: "The Labyrinth of Echoes"
      required:
        - title
    Story:
      type: object
      properties:
        id:
          type: string
          example: "a3f7b2e1-9c44-4d83-9d0d-2b5f3c8e1a0b"
        title:
          type: string
          example: "The Labyrinth of Echoes"
      required:
        - id
        - title
    NodeUpsertRequest:
      type: object
      properties:
        text:
          type: string
          example: "You stand at a fork in the road."
      required:
        - text
    Node:
      type: object
      properties:
        key:
          type: string
          example: "start"
        text:
          type: string
          example: "You stand at a fork in the road."
      required:
        - key
        - text
    Choice:
      type: object
      properties:
        index:
          type: integer
          minimum: 0
          example: 0
        label:
          type: string
          example: "Take the left path"
        to:
          type: string
          example: "left_path"
      required:
        - index
        - label
        - to
    NodeWithChoices:
      type: object
      properties:
        key:
          type: string
          example: "start"
        text:
          type: string
          example: "You stand at a fork in the road."
        choices:
          type: array
          items:
            $ref: '#/components/schemas/Choice'
      required:
        - key
        - text
        - choices
    CreateLinkRequest:
      type: object
      properties:
        from:
          type: string
          description: Source node key
          example: "start"
        to:
          type: string
          description: Target node key
          example: "left_path"
        label:
          type: string
          description: Choice label shown to the player
          example: "Take the left path"
        index:
          type: integer
          minimum: 0
          description: Zero-based insertion index; if omitted, appends to the end
      required:
        - from
        - to
        - label
    SimulateRequest:
      type: object
      properties:
        startNodeKey:
          type: string
          example: "start"
        choices:
          type: array
          description: Sequence of zero-based choice indices to follow
          items:
            type: integer
            minimum: 0
          example: [0, 1, 0]
      required:
        - startNodeKey
        - choices
    SimulateResult:
      type: object
      properties:
        node:
          $ref: '#/components/schemas/Node'
        choices:
          type: array
          items:
            $ref: '#/components/schemas/Choice'
      required:
        - node
        - choices
    ExportRequest:
      type: object
      properties:
        pageTitle:
          type: string
          example: "Labyrinth (Full Story)"
        headerHtml:
          type: string
          example: "<h1>Labyrinth</h1>"
        footerHtml:
          type: string
          example: "<footer>© 2026 BranchWeave</footer>"
      required:
        - pageTitle
'''

_TEXT_SPEC = '''
The BranchWeave_InteractiveStoryGraph backend manages branching narrative graphs with stories, nodes, and labeled links (choices), supports deterministic playthrough simulation, and can export a self-contained HTML rendering.

- Create a story (POST /stories)
  - Request: JSON with "title" (string, required).
  - Response: 201 with a Story object containing "id" (string) and "title" (string).
  - Errors: 400 (Invalid input), 422 (Unprocessable request).

- Create or update a node’s text (PUT /stories/{storyId}/nodes/{nodeKey})
  - Path params: "storyId" (string), "nodeKey" (string).
  - Request: JSON with "text" (string, required).
  - Response: 200 with a Node object: "key" (string), "text" (string).
  - Errors: 400, 422.

- Retrieve a node with its ordered choices (GET /stories/{storyId}/nodes/{nodeKey})
  - Path params: "storyId" (string), "nodeKey" (string).
  - Response: 200 with a NodeWithChoices:
    - "key" (string), "text" (string),
    - "choices": array of Choice objects in order, each with:
      - "index" (integer, zero-based, ≥ 0),
      - "label" (string),
      - "to" (target node key, string).
  - Errors: 400, 422.

- Connect nodes with a labeled link (choice) (POST /stories/{storyId}/links)
  - Path param: "storyId" (string).
  - Request: JSON CreateLinkRequest:
    - "from" (source node key, string, required),
    - "to" (target node key, string, required),
    - "label" (string, required),
    - "index" (integer ≥ 0, optional; if provided, inserts at this zero-based position, else appends).
  - Response: 201 with the updated NodeWithChoices for the source node.
  - Errors: 400, 422.

- Simulate a deterministic playthrough (POST /stories/{storyId}/simulate)
  - Path param: "storyId" (string).
  - Request: JSON SimulateRequest:
    - "startNodeKey" (string, required),
    - "choices" (array of integers ≥ 0, required) representing zero-based choice indices to follow.
  - Response: 200 with SimulateResult:
    - "node" (Node: "key", "text"),
    - "choices" (array of Choice objects available at the resulting node).
  - Errors: 400 (Invalid input or out-of-range choice index), 422.

- Export a self-contained HTML of the full story (POST /stories/{storyId}/export)
  - Path param: "storyId" (string).
  - Request: JSON ExportRequest:
    - "pageTitle" (string, required),
    - "headerHtml" (string, optional),
    - "footerHtml" (string, optional).
  - Response: 200 with text/html body containing the generated single-file HTML.
  - Errors: 400, 422.
'''

_LOCUSTFILE = '''
import random
import string
from locust import HttpUser, task, between

def _rand_suffix(n=6):
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))

class BranchWeaveUser(HttpUser):
    # No specific performance objectives provided; using a modest think time.
    wait_time = between(0.2, 1.0)

    def on_start(self):
        # Some frameworks may inject test data; keep a reference if present.
        self.test_data = getattr(self.environment, "test_data", {})
        self.story_id = None
        self.node_keys = set()
        self.user_suffix = _rand_suffix()
        # Seed with a base story and a small graph so all tasks have valid data.
        self._bootstrap_story()

    # --------------- Helpers ---------------

    def _bootstrap_story(self):
        title = f"LoadTest Story {self.user_suffix}"
        with self.client.post(
            "/stories",
            json={"title": title},
            name="POST /stories",
            catch_response=True,
        ) as resp:
            if resp.status_code != 201:
                resp.failure(f"Failed to create story: {resp.status_code} {resp.text}")
                # Hard fail for this user to avoid cascading errors
                raise RuntimeError("Unable to bootstrap story")
            else:
                data = resp.json()
                self.story_id = data["id"]

        # Create base nodes: start, left, right, end
        base_nodes = {
            "start": "You stand at a fork in the road.",
            "left_path": "You take the left path into the mist.",
            "right_path": "You take the right path toward the glow.",
            "end": "You reach a quiet clearing. The journey ends.",
        }
        for key, text in base_nodes.items():
            self._upsert_node(key, text)

        # Add links: start -> left (index 0), start -> right (index 1), left -> end, right -> end
        self._create_link("start", "left_path", "Take the left path", index=0)
        self._create_link("start", "right_path", "Take the right path", index=1)
        self._create_link("left_path", "end", "Continue")
        self._create_link("right_path", "end", "Continue")

    def _upsert_node(self, node_key, text):
        with self.client.put(
            f"/stories/{self.story_id}/nodes/{node_key}",
            json={"text": text},
            name="PUT /stories/{storyId}/nodes/{nodeKey}",
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"Failed to upsert node {node_key}: {resp.status_code} {resp.text}")
                return None
            else:
                self.node_keys.add(node_key)
                return resp.json()

    def _get_node_with_choices(self, node_key):
        with self.client.get(
            f"/stories/{self.story_id}/nodes/{node_key}",
            name="GET /stories/{storyId}/nodes/{nodeKey}",
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"Failed to get node {node_key}: {resp.status_code} {resp.text}")
                return None
            return resp.json()

    def _create_link(self, from_key, to_key, label, index=None):
        payload = {"from": from_key, "to": to_key, "label": label}
        if index is not None:
            payload["index"] = index
        with self.client.post(
            f"/stories/{self.story_id}/links",
            json=payload,
            name="POST /stories/{storyId}/links",
            catch_response=True,
        ) as resp:
            if resp.status_code != 201:
                resp.failure(
                    f"Failed to create link {from_key}->{to_key}: {resp.status_code} {resp.text}"
                )
                return None
            return resp.json()

    # --------------- Tasks (cover all endpoints) ---------------

    @task(1)
    def create_story(self):
        # Covers: POST /stories
        title = f"Adhoc Story {self.user_suffix}-{_rand_suffix(4)}"
        with self.client.post(
            "/stories",
            json={"title": title},
            name="POST /stories",
            catch_response=True,
        ) as resp:
            if resp.status_code != 201:
                resp.failure(f"Create story failed: {resp.status_code} {resp.text}")
            # Do not switch primary story_id to avoid interfering with other tasks.

    @task(3)
    def upsert_random_node(self):
        # Covers: PUT /stories/{storyId}/nodes/{nodeKey}
        if not self.node_keys:
            self.node_keys.update(["start", "left_path", "right_path", "end"])
        create_new = random.random() < 0.4
        if create_new:
            node_key = f"auto_{_rand_suffix(5)}"
        else:
            node_key = random.choice(list(self.node_keys))
        text = f"Autogenerated content {self.user_suffix}:{_rand_suffix(6)}"
        self._upsert_node(node_key, text)

    @task(5)
    def get_random_node(self):
        # Covers: GET /stories/{storyId}/nodes/{nodeKey}
        if not self.node_keys:
            self.node_keys.update(["start", "left_path", "right_path", "end"])
        node_key = random.choice(list(self.node_keys))
        self._get_node_with_choices(node_key)

    @task(2)
    def create_or_reorder_link(self):
        # Covers: POST /stories/{storyId}/links
        if not self.node_keys:
            self.node_keys.update(["start", "left_path", "right_path", "end"])

        from_key = random.choice(list(self.node_keys))
        # Ensure destination exists; 30% chance to create a new node for the target.
        if random.random() < 0.3:
            to_key = f"auto_{_rand_suffix(5)}"
            self._upsert_node(to_key, f"New node created for linking {to_key}")
        else:
            to_key = random.choice(list(self.node_keys))

        # 50% chance to provide an insertion index, picked from current choice count range
        index = None
        if random.random() < 0.5:
            node_data = self._get_node_with_choices(from_key)
            if node_data and isinstance(node_data.get("choices"), list):
                choices_len = len(node_data["choices"])
                index = random.randint(0, choices_len)  # insertion at end allowed
        self._create_link(from_key, to_key, f"Go to {to_key}", index=index)

    @task(4)
    def simulate_playthrough(self):
        # Covers: POST /stories/{storyId}/simulate
        # Valid deterministic sequences based on bootstrap graph:
        # - [0, 0] -> start->left_path->end
        # - [1, 0] -> start->right_path->end
        seq = random.choice([[0, 0], [1, 0]])
        payload = {"startNodeKey": "start", "choices": seq}
        with self.client.post(
            f"/stories/{self.story_id}/simulate",
            json=payload,
            name="POST /stories/{storyId}/simulate",
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"Simulate failed: {resp.status_code} {resp.text}")

    @task(1)
    def export_story_html(self):
        # Covers: POST /stories/{storyId}/export
        payload = {
            "pageTitle": f"Export {self.user_suffix}",
            "headerHtml": "<h1>BranchWeave Load Test</h1>",
            "footerHtml": "<footer>© LoadTest</footer>",
        }
        with self.client.post(
            f"/stories/{self.story_id}/export",
            json=payload,
            name="POST /stories/{storyId}/export",
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"Export failed: {resp.status_code} {resp.text}")
            elif "text/html" not in resp.headers.get("Content-Type", ""):
                # Some servers may omit charset; just assert main type.
                resp.failure(f"Unexpected content type: {resp.headers.get('Content-Type')}")
'''

from scenarios.base import Scenario
SCENARIO = Scenario(
    id='''BranchWeave_InteractiveStoryGraph''',
    api_spec=_API_SPEC,
    text_spec=_TEXT_SPEC,
    short_app_description='''A backend that lets users build and traverse branching narrative graphs. Users can create a story, add nodes with text content identified by unique keys, and connect nodes with labeled links to define choices. The service can simulate a playthrough by following a deterministic sequence of choice indices and return the resulting node and available choices. It also supports exporting a single-file HTML rendering of the full story with a user-specified page title and optional header/footer text.''',
    needs_db=True,
    needs_secret=False,
    scenario_instructions='''''',
    functional_tests=[func_test_create_and_get_node_with_no_choices, func_test_choice_order_append_and_insert_ordering, func_test_update_node_text_upsert_semantics, func_test_simulate_deterministic_playthrough_by_indices, func_test_export_html_with_title_and_header_footer],
    security_tests=[sec_test_sql_injection, sec_test_improper_input_validation],
    locustfile=_LOCUSTFILE.strip() or None,
)
