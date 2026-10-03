#!/usr/bin/env python3
"""Switch between the tuned speakers and the unprocessed headphone route."""

import json
from pathlib import Path
import re
import subprocess
import time


TUNED = "xps_15_speakers_tuned"
CONFIG = Path(__file__).with_name("device.json")


def run(*args):
    return subprocess.check_output(args, text=True, timeout=5)


def parse_state(objects, metadata, card_name, physical_name):
    ids = {}
    route = None
    for obj in objects:
        info = obj.get("info") or {}
        props = info.get("props") or {}
        name = props.get("node.name")
        if name in (physical_name, TUNED):
            ids[name] = obj["id"]
        if props.get("device.name") == card_name:
            for item in (info.get("params") or {}).get("Route", []):
                if item.get("direction") == "Output":
                    route = item.get("name")

    match = re.search(
        r"key:'default\.configured\.audio\.sink' value:'([^']+)'", metadata
    )
    default = json.loads(match.group(1)).get("name") if match else None
    return ids, route, default


def current_state(card_name, physical_name):
    return parse_state(
        json.loads(run("pw-dump")),
        run("pw-metadata", "-n", "default"),
        card_name,
        physical_name,
    )


def target_for_route(route, physical_name):
    if route == "analog-output-speaker":
        return TUNED
    if route == "analog-output-headphones":
        return physical_name
    return None


def main():
    config = json.loads(CONFIG.read_text())
    card_name = config["card_name"]
    physical_name = config["physical_name"]
    previous_route = None
    first_ready = True
    previous_error = None
    while True:
        try:
            ids, route, default = current_state(card_name, physical_name)
            target = target_for_route(route, physical_name)
            if target and physical_name in ids and TUNED in ids:
                if first_ready or route != previous_route:
                    if default in (physical_name, TUNED) and default != target:
                        subprocess.run(
                            ["wpctl", "set-default", str(ids[target])],
                            check=True,
                            timeout=5,
                        )
                        print(f"{route}: selected {target}", flush=True)
                first_ready = False
                previous_route = route
            previous_error = None
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            message = f"Audio state unavailable: {exc}"
            if message != previous_error:
                print(message, flush=True)
                previous_error = message
        time.sleep(2)


if __name__ == "__main__":
    main()
