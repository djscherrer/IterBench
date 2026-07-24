import json
import os

from workspace.scenario_builder_paths import latest_index, snapshot_path, spec_path
from config import args, logger, scenario_folder_path
from export.render import export_scenario_code


def export_latest_snapshot() -> None:
    """
    Export the most up-to-date snapshot into a scenarios directory.

    - Prefers the latest security snapshot (iw) if present, otherwise latest functional (iu).
    - Exports a single python module named <scenario>.py containing SCENARIO.
    - If a performance snapshot (ip) exists, merges its locust script into the exported module.
    """
    out_dir = getattr(args, "export_dir", None) or os.path.join("src", "scenarios")
    os.makedirs(out_dir, exist_ok=True)

    stem = args.scenario
    iw_latest = latest_index(scenario_folder_path, "iw")
    iu_latest = latest_index(scenario_folder_path, "iu")
    ip_latest = latest_index(scenario_folder_path, "ip")

    # Choose scenario snapshot for correctness tests
    sec = False
    if iw_latest is not None:
        snap_path = snapshot_path(scenario_folder_path, "iw", iw_latest)
        sec = True
    elif iu_latest is not None:
        snap_path = snapshot_path(scenario_folder_path, "iu", iu_latest)
    else:
        snap_path = spec_path(scenario_folder_path)

    with open(snap_path, "r", encoding="utf-8") as f:
        scenario = json.load(f)

    # If a performance snapshot exists, merge locust script into the scenario dict so the
    # exporter can embed it as _LOCUSTFILE and wire it into SCENARIO.locustfile.
    if ip_latest is not None:
        ip_path = snapshot_path(scenario_folder_path, "ip", ip_latest)
        try:
            with open(ip_path, "r", encoding="utf-8") as f:
                ip_scenario = json.load(f)
            if ip_scenario.get("locust_script"):
                scenario["locust_script"] = ip_scenario["locust_script"]
        except Exception as e:
            logger.warning("Could not merge locust script from latest ip snapshot: %s", e)

    # Export SCENARIO module
    export_scenario_code(
        scenario,
        it=0,
        write=True,
        sec=sec,
        out_dir=out_dir,
        filename=f"{stem}.py",
    )
    logger.info("Exported latest scenario snapshot to %s", os.path.join(out_dir, f"{stem}.py"))

