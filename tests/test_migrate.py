"""Exercise migration against real temporary files and a simulated audio session."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("xps_install_migration", ROOT / "install.py")
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        paths = {
            "STATE_DIR": self.home / "state",
            "STATE_FILE": self.home / "state/install.json",
            "LIB_DIR": self.home / "lib",
            "FILTER_FILE": self.home / "pipewire/xps-15-speakers.conf",
            "LEGACY_FILTER_FILE": self.home / "pipewire/filter-chain.conf.d/xps-15-speakers.conf",
            "EQ_UNIT": self.home / "systemd/xps-speaker-eq.service",
            "ROUTE_UNIT": self.home / "systemd/xps-speaker-route.service",
        }
        for name, path in paths.items():
            self.enterContext(patch.object(install, name, path))
            path.parent.mkdir(parents=True, exist_ok=True)
        self.enterContext(patch.object(install.os, "geteuid", return_value=1000))
        self.output = self.enterContext(patch("sys.stdout", new_callable=io.StringIO))
        self.card = {"id": 64, "info": {"props": {"device.name": "alsa_card.pci-0000_00_1f.3"}}}
        self.physical = "alsa_output.pci-0000_00_1f.3.analog-stereo"
        self.originals = {
            install.LEGACY_FILTER_FILE: b"context.modules = [ { name = libpipewire-module-filter-chain } ]\n",
            install.EQ_UNIT: b"[Service]\nExecStart=/usr/bin/pipewire -c filter-chain.conf\n",
        }
        self.state = {
            "card_name": "alsa_card.pci-0000_00_1f.3",
            "physical_name": self.physical,
            "old_default": self.physical,
            "old_volume": 0.42,
            "services": {},
            "files": [],
        }
        for index, (path, data) in enumerate(self.originals.items()):
            path.write_bytes(data)
            path.chmod(0o640)
            backup = f"backup-{index}"
            (install.STATE_DIR / backup).write_bytes(b"pre-install " + data)
            self.state["files"].append({"target": str(path), "backup": backup, "sha256": install.digest(data)})
        install.STATE_FILE.write_text(json.dumps(self.state))
        self.original_state = install.STATE_FILE.read_bytes()
        self.services = {
            name: {"active": True, "enabled": True}
            for name in (*install.UNITS, "filter-chain.service")
        }
        self.tuned = {"id": 77, "info": {"props": {"node.name": install.TUNED}}}
        self.external = {"id": 99, "info": {"props": {"node.name": "bluetooth.output"}}}
        self.nodes = [self.tuned, self.external]
        self.current_default = install.TUNED
        self.commands = []
        self.shared_filter_contents = []
        self.fail_start = False
        self.call_mock = self.enterContext(patch.object(install, "call", side_effect=self.call))
        self.enterContext(patch.object(install, "service_state", side_effect=lambda units=install.UNITS: {
            unit: copy.deepcopy(self.services[unit]) for unit in units
        }))
        self.enterContext(patch.object(install, "default_name", side_effect=lambda: self.current_default))
        self.enterContext(patch.object(install, "objects", side_effect=lambda: self.nodes))
        self.enterContext(patch.object(install, "wait_for_node", return_value=self.tuned))

    def call(self, *args, **kwargs):
        self.commands.append(args)
        if args == ("systemctl", "--user", "restart", "filter-chain.service"):
            self.shared_filter_contents.append(install.LEGACY_FILTER_FILE.read_bytes())
        if args[:2] == ("wpctl", "get-volume"):
            return "Volume: 0.36 [MUTED]\n"
        if self.fail_start and args == ("systemctl", "--user", "start", install.UNITS[0]):
            self.fail_start = False
            raise install.InstallError("simulated EQ startup failure")
        return ""

    def test_migrates_once_and_keeps_uninstall_backups(self):
        install.migrate()
        self.assertEqual(install.LEGACY_FILTER_FILE.read_bytes(), install.LEGACY_PLACEHOLDER)
        self.assertIn(b"libpipewire-module-protocol-native", install.FILTER_FILE.read_bytes())
        self.assertIn(self.physical.encode(), install.FILTER_FILE.read_bytes())
        self.assertIn(b"xps-15-speakers.conf", install.EQ_UNIT.read_bytes())
        state = json.loads(install.STATE_FILE.read_text())
        self.assertEqual(state["old_volume"], self.state["old_volume"])
        self.assertEqual(state["old_default"], self.state["old_default"])
        for old, new in zip(self.state["files"], state["files"]):
            self.assertEqual(new["backup"], old["backup"])
            self.assertEqual((install.STATE_DIR / old["backup"]).read_bytes(),
                             b"pre-install " + self.originals[Path(old["target"])])
        self.assertEqual(state["files"][-1]["target"], str(install.FILTER_FILE))
        self.assertIsNone(state["files"][-1]["backup"])
        for entry in state["files"]:
            self.assertEqual(install.digest(Path(entry["target"]).read_bytes()), entry["sha256"])
        self.assertIn(("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"), self.commands)
        self.assertIn(("wpctl", "set-volume", "77", "0.36"), self.commands)
        self.assertIn(("wpctl", "set-mute", "77", "1"), self.commands)
        self.assertIn(("wpctl", "set-default", "77"), self.commands)
        backups = list(install.STATE_DIR.glob("migration-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "install.json").read_bytes(), self.original_state)
        self.commands.clear()
        installed_state = install.STATE_FILE.read_bytes()
        install.migrate()
        self.assertEqual(install.STATE_FILE.read_bytes(), installed_state)
        self.assertEqual(self.commands, [])
        self.assertEqual(len(list(install.STATE_DIR.glob("migration-*"))), 1)

    def test_dry_run_does_not_write_or_contact_audio_services(self):
        install.migrate(dry_run=True)
        self.assertEqual(install.STATE_FILE.read_bytes(), self.original_state)
        self.assertFalse(install.FILTER_FILE.exists())
        self.assertEqual(list(install.STATE_DIR.glob("migration-*")), [])
        for path, data in self.originals.items():
            self.assertEqual(path.read_bytes(), data)
        self.call_mock.assert_not_called()

    def test_refuses_edited_managed_file(self):
        install.LEGACY_FILTER_FILE.write_bytes(b"my custom EQ")
        with self.assertRaisesRegex(install.InstallError, "edited"):
            install.migrate()
        self.assertEqual(install.LEGACY_FILTER_FILE.read_bytes(), b"my custom EQ")
        self.assertFalse(install.FILTER_FILE.exists())
        self.call_mock.assert_not_called()

    def test_refuses_unrecorded_private_config(self):
        install.FILTER_FILE.write_bytes(b"another private EQ")
        with self.assertRaisesRegex(install.InstallError, "unrecorded"):
            install.migrate()
        self.assertEqual(install.FILTER_FILE.read_bytes(), b"another private EQ")
        self.call_mock.assert_not_called()

    def test_refuses_symlink_for_managed_file(self):
        unit_copy = self.home / "unit-copy"
        install.EQ_UNIT.rename(unit_copy)
        install.EQ_UNIT.symlink_to(unit_copy)
        with self.assertRaises(install.InstallError):
            install.migrate()
        self.assertTrue(install.EQ_UNIT.is_symlink())
        self.call_mock.assert_not_called()

    def test_refuses_missing_installation_record(self):
        install.STATE_FILE.unlink()
        with self.assertRaisesRegex(install.InstallError, "No installation record"):
            install.migrate()
        self.call_mock.assert_not_called()

    def test_preserves_external_default(self):
        self.current_default = "bluetooth.output"
        install.migrate()
        self.assertIn(("wpctl", "set-default", "99"), self.commands)
        self.assertNotIn(("wpctl", "set-default", "77"), self.commands)

    def test_does_not_start_inactive_shared_service(self):
        self.services["filter-chain.service"]["active"] = False
        install.migrate()
        self.assertFalse(any("filter-chain.service" in args for args in self.commands))

    def test_service_failure_restores_files_state_and_selection(self):
        self.fail_start = True
        with self.assertRaisesRegex(install.InstallError, "previous files and services restored"):
            install.migrate()
        self.assertEqual(install.STATE_FILE.read_bytes(), self.original_state)
        self.assertFalse(install.FILTER_FILE.exists())
        for path, data in self.originals.items():
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertIn(("wpctl", "set-volume", "77", "0.36"), self.commands)
        self.assertIn(("wpctl", "set-mute", "77", "1"), self.commands)
        self.assertIn(("wpctl", "set-default", "77"), self.commands)

    def test_duplicate_output_after_restart_rolls_back(self):
        self.nodes = [self.tuned, self.tuned]
        with self.assertRaisesRegex(install.InstallError, "Expected one tuned output"):
            install.migrate()
        self.assertFalse(install.FILTER_FILE.exists())
        self.assertEqual(install.STATE_FILE.read_bytes(), self.original_state)

    def test_state_write_failure_rolls_back(self):
        with patch.object(Path, "replace", side_effect=OSError("simulated state write failure")):
            with self.assertRaisesRegex(install.InstallError, "previous files and services restored"):
                install.migrate()
        self.assertEqual(install.STATE_FILE.read_bytes(), self.original_state)
        self.assertFalse(install.FILTER_FILE.exists())
        for path, data in self.originals.items():
            self.assertEqual(path.read_bytes(), data)

    def test_rollback_leaves_previously_inactive_services_stopped(self):
        self.services[install.UNITS[1]]["active"] = False
        self.services["filter-chain.service"]["active"] = False
        self.fail_start = True
        with self.assertRaises(install.InstallError):
            install.migrate()
        self.assertNotIn(("systemctl", "--user", "start", install.UNITS[1]), self.commands)
        self.assertFalse(any("filter-chain.service" in args for args in self.commands))

    def test_uninstall_restores_pre_install_files_after_migration(self):
        install.migrate()
        state = json.loads(install.STATE_FILE.read_text())
        install.restore_files(state)
        self.assertFalse(install.FILTER_FILE.exists())
        for path, data in self.originals.items():
            self.assertEqual(path.read_bytes(), b"pre-install " + data)

    def test_install_and_uninstall_preserve_legacy_manual_setup(self):
        shutil.rmtree(install.STATE_DIR)
        physical_node = {"id": 68, "info": {"props": {"node.name": self.physical}}}
        self.nodes.append(physical_node)
        with patch.object(install, "detect", return_value=(self.card, self.nodes, 0)), \
             patch.object(install, "output_route", return_value="analog-output-speaker"), \
             patch.object(install, "profile_name", return_value="output:analog-stereo"), \
             patch.object(install, "switch_on", return_value=["on", "on"]), \
             patch.object(install, "restore_audio"), \
             patch.object(install, "wait_for_node", side_effect=lambda name: (
                 physical_node if name == self.physical else self.tuned
             )):
            install.install(replace_existing=True)
            self.assertEqual(install.LEGACY_FILTER_FILE.read_bytes(), install.LEGACY_PLACEHOLDER)
            state = json.loads(install.STATE_FILE.read_text())
            entry = next(e for e in state["files"] if e["target"] == str(install.LEGACY_FILTER_FILE))
            self.assertEqual((install.STATE_DIR / entry["backup"]).read_bytes(),
                             self.originals[install.LEGACY_FILTER_FILE])
            install.uninstall()
        for path, data in self.originals.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertFalse(install.FILTER_FILE.exists())
        self.assertFalse(install.STATE_DIR.exists())
        self.assertEqual(self.shared_filter_contents,
                         [install.LEGACY_PLACEHOLDER, self.originals[install.LEGACY_FILTER_FILE]])

    def test_manual_install_requires_explicit_replacement(self):
        shutil.rmtree(install.STATE_DIR)
        with patch.object(install, "detect", return_value=(self.card, self.nodes, 0)), \
             patch.object(install, "output_route", return_value="analog-output-speaker"):
            with self.assertRaisesRegex(install.InstallError, "replace-existing"):
                install.install()
        for path, data in self.originals.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertFalse(install.STATE_DIR.exists())
        self.call_mock.assert_not_called()

    def test_fresh_install_does_not_create_shared_fragment(self):
        install.LEGACY_FILTER_FILE.unlink()
        files, _ = install.target_files(self.card)
        self.assertNotIn(install.LEGACY_FILTER_FILE, files)
        self.assertFalse(install.shared_filter_active(files))


if __name__ == "__main__":
    unittest.main()
