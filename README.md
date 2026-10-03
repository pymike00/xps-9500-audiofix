# XPS 15 9500 speaker tuning for Linux

A small, rootless PipeWire setup for the Dell XPS 15 9500. It enables a speaker-only equalizer, keeps the normal Analog Stereo profile, and switches wired headphones back to the unprocessed output. The settings were tuned by listening on one XPS 15 9500 running Ubuntu 26.04.1, kernel 7.0.0-38, PipeWire 1.6.2, and WirePlumber 0.5.13.

## What it fixes

The laptop has four physical speaker drivers, but its Realtek ALC289 exposes a **stereo** speaker path. The kernel's `ALC289_FIXUP_DUAL_SPK` fix for Dell codec subsystem `1028:097d` enables the second speaker pin (`0x17`, the bass speakers) and connects it to the speaker DAC. The generic **Analog Surround 4.0** profile is not a four-driver crossover for this laptop; use **Analog Stereo**. See the [Linux Realtek codec source](https://github.com/torvalds/linux/blob/master/sound/hda/codecs/realtek/alc269.c).

The filter protects the very low end with a 75 Hz high-pass, adds 2.0 dB around 120 Hz and 3.3 dB around 190 Hz, slightly softens the top end, and leaves processing headroom. These are subjective settings from one machine, not a measured calibration. The upper and lower speaker pairs receive the same stereo program; this software EQ cannot independently send bass to the lower pair.

Dell [lists subwoofer support](https://www.dell.com/support/manuals/en-ed/xps-15-9500-laptop/xps-15-9500-setup-and-specifications/audio?guid=guid-6878b68f-ccfb-4c6a-9f62-3ed941403f53&lang=en-us) but does not publish a 60 Hz cutoff. [Notebookcheck's own measurement](https://www.notebookcheck.net/Dell-XPS-15-9500-Core-i5-Review-Now-Even-More-Like-a-MacBook-Pro.466868.0.html) shows bass rolling off from about 200 Hz and evaluates bass from 100 Hz upward. Do not expect strong 60 Hz output from these small drivers. Boosting deep bass harder may produce distortion.

## Requirements

- Dell **XPS 15 9500**, with Realtek **ALC289** and codec subsystem **1028:097d**.
- A kernel that applies the `0x17 0x90170130` dual-speaker pin fix. The installer checks this and stops if it is missing.
- PipeWire, WirePlumber, systemd user services, Python 3.8 or newer, `wpctl`, `pw-dump`, `pw-metadata`, and `amixer`.
- A logged-in desktop audio session with the built-in speakers selected and wired headphones unplugged.

The installer needs **no `sudo`** and does not install packages or patch the kernel. It refuses unsupported hardware.

## Install

Download or clone this repository, open a terminal in its directory, then run:

```sh
python3 install.py install --dry-run
python3 install.py install
```

If you already have files at the install locations, the installer stops and lists them. Review those files, then run `python3 install.py install --replace-existing` to back them up and replace them. The option is useful if you previously applied this project's manual configuration. The install script sets the physical speaker level to 90%; use the normal desktop volume control for day-to-day listening and lower it if a track buzzes.

Select **XPS 15 Speakers (Tuned)** in Sound settings if it is not selected automatically. Leave the card profile at **Analog Stereo**. The headphone jack uses the plain Built-in Audio Analog Stereo output when its port becomes active; choosing Bluetooth or HDMI manually is respected.

Check the result:

```sh
python3 install.py status
wpctl status
```

You should see both `xps-speaker-*.service` units active and `xps_15_speakers_tuned` as the configured default while the built-in speaker port is active.

## Uninstall

```sh
python3 install.py uninstall
```

This stops and disables the two user services, restores any files it replaced, and restores the saved profile, volume, switches, and default output where those devices still exist. If you edited an installed file, uninstall stops to avoid discarding your edits. Use `python3 install.py uninstall --force` if you want to discard those edits.

## Files installed

| Path under your home directory | Purpose |
| --- | --- |
| `.config/pipewire/filter-chain.conf.d/xps-15-speakers.conf` | PipeWire speaker filter graph. |
| `.local/lib/xps-9500-audiofix/route.py` and `device.json` | Detect the active speaker/headphone port and choose the matching output. |
| `.config/systemd/user/xps-speaker-eq.service` | Start the filter at login. |
| `.config/systemd/user/xps-speaker-route.service` | Start the route watcher at login. |
| `.local/state/xps-9500-audiofix/` | Installation record and backups for uninstall. |

The EQ uses PipeWire's [built-in filter-chain module](https://pipewire.pages.freedesktop.org/pipewire/page_module_filter_chain.html). The route watcher checks the active PipeWire card route every two seconds. It changes the default only when the current default is one of this laptop's two internal outputs, so an explicitly selected external output stays selected.

## Troubleshooting

- **Installer says the kernel fix is missing:** update to a kernel with the `1028:097d` dual-speaker fix. EQ alone cannot enable a missing speaker pin.
- **No tuned output appears:** run `systemctl --user status xps-speaker-eq.service` and `journalctl --user -u xps-speaker-eq.service -n 30 --no-pager`.
- **Headphones keep the speaker EQ:** run `python3 install.py status` and `journalctl --user -u xps-speaker-route.service -n 30 --no-pager`. The headphone-switching logic was verified against the speaker route on the development laptop; a physical headphone insertion test was not available for this release.
- **Buzzing or harsh sound:** lower the normal speaker volume. This is a moderate EQ, and individual speaker units or tracks may still distort.
- **You want to edit the EQ:** edit the installed `xps-15-speakers.conf`, then run `systemctl --user restart xps-speaker-eq.service`. Uninstall will require `--force` after an edit, so keep a copy of your changes first.

## License

MIT; see [LICENSE](LICENSE).

To run the small route and ALSA parser checks while developing, use `python3 -m unittest discover -s tests`.
