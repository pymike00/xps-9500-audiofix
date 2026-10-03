import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("xps_route", ROOT / "route.py")
route = importlib.util.module_from_spec(spec)
spec.loader.exec_module(route)
install_spec = importlib.util.spec_from_file_location("xps_install", ROOT / "install.py")
install = importlib.util.module_from_spec(install_spec)
install_spec.loader.exec_module(install)


class RouteTests(unittest.TestCase):
    def test_alsa_switch_parser_accepts_volume_fields(self):
        mixer = "Front Left: Playback 87 [100%] [0.00dB] [on]\nFront Right: Playback [off]"
        self.assertEqual(install.playback_switches(mixer), ["on", "off"])

    def test_speaker_and_headphone_routes(self):
        physical = "alsa_output.pci-0000_00_1f.3.analog-stereo"
        self.assertEqual(route.target_for_route("analog-output-speaker", physical), route.TUNED)
        self.assertEqual(route.target_for_route("analog-output-headphones", physical), physical)
        self.assertIsNone(route.target_for_route("hdmi-output-0", physical))

    def test_reads_active_card_route_and_default(self):
        card_name = "alsa_card.pci-0000_00_1f.3"
        physical = "alsa_output.pci-0000_00_1f.3.analog-stereo"
        objects = [
            {
                "id": 51,
                "info": {
                    "props": {"device.name": card_name},
                    "params": {
                        "Route": [
                            {"direction": "Input", "name": "analog-input-internal-mic"},
                            {"direction": "Output", "name": "analog-output-headphones"},
                        ]
                    },
                },
            },
            {"id": 55, "info": {"props": {"node.name": physical}}},
            {"id": 75, "info": {"props": {"node.name": route.TUNED}}},
        ]
        metadata = (
            "update: id:0 key:'default.configured.audio.sink' value:'"
            + json.dumps({"name": route.TUNED})
            + "' type:'Spa:String:JSON'"
        )
        ids, active_route, default = route.parse_state(objects, metadata, card_name, physical)
        self.assertEqual(ids, {physical: 55, route.TUNED: 75})
        self.assertEqual(active_route, "analog-output-headphones")
        self.assertEqual(default, route.TUNED)


if __name__ == "__main__":
    unittest.main()
