#!/usr/bin/env python3
"""Install or remove the XPS 15 9500 speaker tuning without root access."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
HOME = Path.home()
STATE_DIR = HOME / ".local/state/xps-9500-audiofix"
STATE_FILE = STATE_DIR / "install.json"
LIB_DIR = HOME / ".local/lib/xps-9500-audiofix"
FILTER_FILE = HOME / ".config/pipewire/filter-chain.conf.d/xps-15-speakers.conf"
EQ_UNIT = HOME / ".config/systemd/user/xps-speaker-eq.service"
ROUTE_UNIT = HOME / ".config/systemd/user/xps-speaker-route.service"
UNITS = ("xps-speaker-eq.service", "xps-speaker-route.service")
TUNED = "xps_15_speakers_tuned"


class InstallError(Exception):
    pass


def call(*args, capture=False, check=True):
    result = subprocess.run(
        args,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
        timeout=15,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise InstallError(f"{' '.join(args)} failed: {detail}")
    return result.stdout if capture else result.returncode


def objects():
    return json.loads(call("pw-dump", capture=True))


def props(obj):
    return (obj.get("info") or {}).get("props") or {}


def find_node(items, name):
    return next(
        (obj for obj in items if props(obj).get("node.name") == name), None
    )


def output_route(card):
    for route in ((card.get("info") or {}).get("params") or {}).get("Route", []):
        if route.get("direction") == "Output":
            return route.get("name")
    return None


def default_name():
    metadata = call("pw-metadata", "-n", "default", capture=True)
    match = re.search(
        r"key:'default\.configured\.audio\.sink' value:'([^']+)'", metadata
    )
    return json.loads(match.group(1)).get("name") if match else None


def profile_name(card):
    selected = ((card.get("info") or {}).get("params") or {}).get("Profile", [])
    return selected[0].get("name") if selected else None


def detect():
    model_path = Path("/sys/class/dmi/id/product_name")
    model = model_path.read_text().strip() if model_path.exists() else "unknown"
    if model != "XPS 15 9500":
        raise InstallError(f"Unsupported model: {model!r}; expected 'XPS 15 9500'.")

    items = objects()
    cards = [
        obj
        for obj in items
        if obj.get("type") == "PipeWire:Interface:Device"
        and "HDA:10ec0289,1028097d" in props(obj).get("alsa.components", "")
    ]
    if len(cards) != 1:
        raise InstallError(
            "Expected one ALC289 card with Dell codec subsystem 1028:097d. "
            "Check that PipeWire and WirePlumber are running."
        )
    card = cards[0]
    card_number = int(props(card)["alsa.card"])
    pin_file = Path(f"/sys/class/sound/hwC{card_number}D0/driver_pin_configs")
    if not pin_file.exists() or "0x17 0x90170130" not in pin_file.read_text():
        raise InstallError(
            "The kernel's dual-speaker pin fix is missing. Update to a kernel "
            "with the 1028:097d ALC289 fix before installing this EQ."
        )
    return card, items, card_number


def stereo_profile(card):
    params = (card.get("info") or {}).get("params") or {}
    profiles = params.get("EnumProfile", [])
    for name in ("output:analog-stereo+input:analog-stereo", "output:analog-stereo"):
        match = next(
            (p for p in profiles if p.get("name") == name and p.get("available") != "no"),
            None,
        )
        if match:
            return match["index"]
    raise InstallError("The Analog Stereo profile is not available for this card.")


def wait_for_node(name, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        node = find_node(objects(), name)
        if node:
            return node
        time.sleep(0.2)
    raise InstallError(f"Timed out waiting for PipeWire node {name!r}.")


def volume(node):
    if not node:
        return None
    result = call("wpctl", "get-volume", str(node["id"]), capture=True)
    match = re.search(r"Volume:\s*([0-9.]+)", result)
    return float(match.group(1)) if match else None


def playback_switches(result):
    return [
        match.group(1)
        for line in result.splitlines()
        if "Playback" in line
        if (match := re.search(r"\[(on|off)\]\s*$", line))
    ]


def switch_on(card_number, control):
    result = call("amixer", "-c", str(card_number), "sget", control, capture=True)
    switches = playback_switches(result)
    if not switches:
        raise InstallError(f"Missing ALSA playback switch {control!r}.")
    if len(set(switches)) > 1:
        raise InstallError(f"Mixed left/right state for {control!r} is not supported.")
    if any(value == "off" for value in switches):
        call("amixer", "-c", str(card_number), "sset", control, "on", capture=True)
    return switches


def restore_switch(card_number, control, switches):
    if switches and all(value == "off" for value in switches):
        call("amixer", "-c", str(card_number), "sset", control, "off", capture=True)


def service_state():
    return {
        unit: {
            "active": call("systemctl", "--user", "is-active", unit, capture=True, check=False).strip()
            == "active",
            "enabled": call("systemctl", "--user", "is-enabled", unit, capture=True, check=False).strip()
            == "enabled",
        }
        for unit in UNITS
    }


def target_files(card):
    card_name = props(card)["device.name"]
    if not card_name.startswith("alsa_card."):
        raise InstallError(f"Unexpected card name: {card_name!r}")
    physical = "alsa_output." + card_name[len("alsa_card.") :] + ".analog-stereo"
    template = (ROOT / "config/speaker-filter.conf.in").read_text()
    filter_text = template.replace("@PHYSICAL_JSON@", json.dumps(physical))
    config = {"card_name": card_name, "physical_name": physical}
    files = {
        FILTER_FILE: filter_text.encode(),
        LIB_DIR / "route.py": (ROOT / "route.py").read_bytes(),
        LIB_DIR / "device.json": (json.dumps(config, indent=2) + "\n").encode(),
        EQ_UNIT: (ROOT / "systemd/xps-speaker-eq.service").read_bytes(),
        ROUTE_UNIT: (ROOT / "systemd/xps-speaker-route.service").read_bytes(),
    }
    return files, physical


def digest(data):
    return hashlib.sha256(data).hexdigest()


def restore_files(state):
    for entry in state["files"]:
        target = Path(entry["target"])
        if entry["backup"]:
            backup = STATE_DIR / entry["backup"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
        else:
            target.unlink(missing_ok=True)


def restore_services(state):
    call("systemctl", "--user", "daemon-reload")
    for unit, old in state["services"].items():
        if old["enabled"]:
            call("systemctl", "--user", "enable", unit)
        if old["active"]:
            call("systemctl", "--user", "start", unit)


def restore_audio(state):
    """Return the settings changed by install, if the original devices remain."""
    items = objects()
    card = next(
        (obj for obj in items if props(obj).get("device.name") == state["card_name"]),
        None,
    )
    old_profile = state["old_profile"]
    if card and old_profile and profile_name(card) != old_profile:
        match = next(
            (
                p
                for p in ((card.get("info") or {}).get("params") or {}).get("EnumProfile", [])
                if p.get("name") == old_profile
            ),
            None,
        )
        if match:
            call("wpctl", "set-profile", str(card["id"]), str(match["index"]))
    old_volume = state["old_volume"]
    if old_volume is not None:
        sink = find_node(objects(), state["physical_name"])
        if sink:
            call("wpctl", "set-volume", str(sink["id"]), str(old_volume))
    if card:
        current_card_number = int(props(card)["alsa.card"])
        for control, switches in state["old_switches"].items():
            restore_switch(current_card_number, control, switches)
    old_default = state["old_default"]
    if old_default:
        previous = find_node(objects(), old_default)
        if previous and default_name() in (state["physical_name"], TUNED):
            call("wpctl", "set-default", str(previous["id"]))


def install(replace_existing=False, dry_run=False):
    if os.geteuid() == 0:
        raise InstallError("Run this as your normal desktop user, without sudo.")
    for command in ("pipewire", "pw-dump", "pw-metadata", "wpctl", "amixer", "systemctl"):
        if shutil.which(command) is None:
            raise InstallError(f"Required command not found: {command}")
    if STATE_FILE.exists():
        print("Already installed. Use 'status' or 'uninstall' first.")
        return
    if STATE_DIR.exists():
        raise InstallError(f"Incomplete installation state at {STATE_DIR}; inspect it before retrying.")

    card, items, card_number = detect()
    if output_route(card) != "analog-output-speaker":
        raise InstallError("Unplug wired headphones and select built-in speakers before installing.")
    files, physical = target_files(card)
    conflicts = [target for target in files if target.exists() or target.is_symlink()]
    if any(target.is_symlink() for target in conflicts):
        raise InstallError("An install target is a symlink; resolve it before installing.")
    if conflicts and not replace_existing:
        names = "\n  ".join(str(target) for target in conflicts)
        raise InstallError(
            f"Existing files would be replaced:\n  {names}\n"
            "Use --replace-existing to back them up and continue."
        )
    print(f"Compatible model and codec found; kernel woofer fix is active (card {card_number}).")
    print(f"Physical stereo sink: {physical}")
    if dry_run:
        print("Dry run: no files or audio settings changed.")
        return

    old_default = default_name()
    old_profile = profile_name(card)
    old_volume = volume(find_node(items, physical))
    state = {
        "card_number": card_number,
        "card_name": props(card)["device.name"],
        "physical_name": physical,
        "old_default": old_default,
        "old_profile": old_profile,
        "old_volume": old_volume,
        "old_switches": {},
        "services": service_state(),
        "files": [],
    }
    STATE_DIR.mkdir(parents=True, exist_ok=False)
    try:
        for index, (target, data) in enumerate(files.items()):
            backup_name = f"backup-{index}" if target.exists() else None
            if backup_name:
                shutil.copy2(target, STATE_DIR / backup_name)
            state["files"].append(
                {"target": str(target), "backup": backup_name, "sha256": digest(data)}
            )

        call("systemctl", "--user", "stop", UNITS[1], UNITS[0], check=False)
        for target, data in files.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o644)

        if old_profile not in (
            "output:analog-stereo+input:analog-stereo",
            "output:analog-stereo",
        ):
            call("wpctl", "set-profile", str(card["id"]), str(stereo_profile(card)))
        sink = wait_for_node(physical)
        for control in ("Speaker", "Bass Speaker"):
            state["old_switches"][control] = switch_on(card_number, control)
        call("wpctl", "set-volume", str(sink["id"]), "0.90")

        call("systemctl", "--user", "daemon-reload")
        call("systemctl", "--user", "enable", "--now", *UNITS)
        tuned = wait_for_node(TUNED)
        if old_default in (physical, TUNED, None):
            call("wpctl", "set-default", str(tuned["id"]))
        STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")
    except Exception:
        call("systemctl", "--user", "disable", "--now", *UNITS, check=False)
        restore_files(state)
        restore_services(state)
        restore_audio(state)
        shutil.rmtree(STATE_DIR)
        raise
    print("Installed. The XPS 15 Speakers (Tuned) output is ready.")


def uninstall(force=False):
    if os.geteuid() == 0:
        raise InstallError("Run this as your normal desktop user, without sudo.")
    if not STATE_FILE.exists():
        raise InstallError("No installation record found.")
    state = json.loads(STATE_FILE.read_text())
    changed = [
        entry["target"]
        for entry in state["files"]
        if Path(entry["target"]).exists()
        and digest(Path(entry["target"]).read_bytes()) != entry["sha256"]
    ]
    if changed and not force:
        raise InstallError(
            "Installed files were edited; use 'uninstall --force' to remove them:\n  "
            + "\n  ".join(changed)
        )

    physical = state["physical_name"]
    sink = find_node(objects(), physical)
    if sink and default_name() == TUNED:
        call("wpctl", "set-default", str(sink["id"]))
    call("systemctl", "--user", "disable", "--now", *UNITS)
    restore_files(state)
    restore_services(state)
    restore_audio(state)
    shutil.rmtree(STATE_DIR)
    print("Uninstalled; previous files and audio settings were restored.")


def status():
    print("Installed:", "yes" if STATE_FILE.exists() else "no")
    for unit, state in service_state().items():
        print(f"{unit}: {'active' if state['active'] else 'inactive'}")
    try:
        print("Default sink:", default_name() or "unknown")
        card, _, _ = detect()
        print("Output route:", output_route(card) or "unknown")
    except (InstallError, OSError, subprocess.SubprocessError) as exc:
        print("Hardware check:", exc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    install_parser = sub.add_parser("install", help="Install the speaker tuning")
    install_parser.add_argument("--replace-existing", action="store_true")
    install_parser.add_argument("--dry-run", action="store_true")
    uninstall_parser = sub.add_parser("uninstall", help="Restore previous settings")
    uninstall_parser.add_argument("--force", action="store_true")
    sub.add_parser("status", help="Show service and output status")
    args = parser.parse_args()
    try:
        if args.command == "install":
            install(args.replace_existing, args.dry_run)
        elif args.command == "uninstall":
            uninstall(args.force)
        else:
            status()
    except (InstallError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
