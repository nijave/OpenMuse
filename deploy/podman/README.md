# openmuse-capture: overnight Muse recording in rootless Podman

Packages OpenMuse as a rootless Podman quadlet for a headless Pine A64
(aarch64, Fedora 44, BlueZ 5.87). The image entry point is `dashboard.py`, a
supervisor that:

* starts `OpenMuse stream --address $MUSE_ADDRESS --preset $MUSE_PRESET
  --auto-reconnect --record /data/muse_<YYYY-MM-DD_HHMM>.txt --status-file
  /data/status.json --status-interval 5` and passes its output through to
  journald;
* at `MUSE_ROTATE_AT` local time (default 12:00) stops the child (SIGINT,
  then SIGTERM) and starts a new one with a new record file;
* if the child exits for any other reason, the supervisor exits with the
  child's code and `Restart=always` in the quadlet takes over;
* serves a phone-friendly status page on
  `http://<pine>:8088/` (plus `/status.json` with merged supervisor fields
  and `/healthz` for monitoring).

## Files

* `Containerfile` — `python:3.12.16-slim-trixie`, installs the repo with
  `pip install --no-deps` plus `bleak`, `numpy`, `pandas`, `mne-lsl==1.9.0`,
  `paho-mqtt`. Runs as the non-root user `openmuse` (UID 10001).
* `dashboard.py` — supervisor + status dashboard (stdlib only); after each
  rotation (and at startup, for leftovers from a previous run) it compresses
  finished record files in place with `zstd`, so an overnight capture ends up
  as `muse_*.txt.zst` (roughly 2.5x smaller; the live file stays uncompressed
  for crash safety).
* `ha_mqtt.py` — optional Home Assistant MQTT discovery publisher (see
  "Home Assistant over MQTT" below); imported by `dashboard.py` only at
  runtime, and `paho-mqtt` itself is imported only when `MUSE_MQTT_HOST` is
  set, so the dashboard still runs without both.
* `test_dashboard.py`, `test_ha_mqtt.py` — unit tests (stdlib `unittest`, no
  broker, no headband): `python3 -m unittest deploy/podman/test_ha_mqtt.py
  deploy/podman/test_dashboard.py` from the repo root.
* `build.sh` — multi-arch build (`linux/amd64` + `linux/arm64`) into one
  manifest list. Build context is the repo root.
* `openmuse-capture.container` — the quadlet (user unit).
* `openmuse-capture.env.example` — example environment file.

`.containerignore` at the repo root keeps `.git`, `secret/`, `tests/`,
`validation/`, `media/` and `*.txt` captures out of the image.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MUSE_ADDRESS` | required | headband MAC address |
| `MUSE_PRESET` | `p1035` | preset passed to `OpenMuse stream` |
| `MUSE_DATA_DIR` | `/data` | record files and `status.json` |
| `MUSE_DASHBOARD_PORT` | `8088` | dashboard HTTP port |
| `MUSE_DASHBOARD_BIND` | `0.0.0.0` | dashboard bind address |
| `MUSE_ROTATE_AT` | `12:00` | daily record rotation, local `HH:MM`; empty disables |
| `MUSE_RECONNECT_DELAY` | `10` | seconds between stream reconnects |
| `MUSE_RECONNECT_ATTEMPTS` | `1000000` | stream reconnect attempts |
| `MUSE_EXTRA_ARGS` | empty | extra `OpenMuse stream` args (shlex-split) |
| `MUSE_COMPRESS` | `zstd` | compressor used for finished record files; empty or `none` disables compression |
| `MUSE_COMPRESS_LEVEL` | `3` | zstd compression level (1–19) |
| `MUSE_MQTT_HOST` | empty | MQTT broker host; empty (default) disables MQTT entirely |
| `MUSE_MQTT_PORT` | `1883` | MQTT broker port |
| `MUSE_MQTT_USER` | empty | MQTT username (optional) |
| `MUSE_MQTT_PASSWORD` | empty | MQTT password |
| `MUSE_MQTT_PREFIX` | `openmuse` | MQTT topic prefix, topics are `<prefix>/<device-id>/...` |
| `MUSE_HA_DISCOVERY_PREFIX` | `homeassistant` | Home Assistant MQTT discovery prefix |
| `MUSE_MQTT_INTERVAL` | `10` | seconds between MQTT state publications (changes publish immediately) |

Finished (rotated or leftover) `muse_*.txt` recordings are compressed to
`muse_*.txt.zst` a minute or so after the child stops writing them; the file
the live child is writing is never touched. Compression activity shows on the
dashboard page and under `supervisor` in `/status.json` (`compressing` and
`compress`). Read the `.zst` files back with
`OpenMuse.read_rawdata_lines("muse_....txt.zst")`.

## Home Assistant over MQTT

Set `MUSE_MQTT_HOST` (and credentials if the broker needs them) to expose the
capture to Home Assistant through its MQTT discovery integration — the
headband appears as one device ("Muse S Athena <last 4 MAC octets>",
manufacturer Interaxon) with these entities:

| Entity | Component | Device class | Unit | Source |
|---|---|---|---|---|
| Capture state | `sensor` | `enum` (7 states + unknown) | – | `status.json` `state` (or `not-running` when the child is dead) |
| Headband connected | `binary_sensor` | `connectivity` | – | `status.json` `connected` |
| Battery | `sensor` | `battery` | % | `battery_percent` |
| Battery time to empty | `sensor` | `duration` | s | `battery_time_to_empty_s` |
| Seconds since last packet | `sensor` | `duration` | s | `last_data_age_s` (diagnostic) |
| EEG samples | `sensor` | – | samples | `samples.EEG`, cumulative per process (diagnostic) |
| Recording size | `sensor` | `data_size` | B | `record_bytes` (diagnostic; HA converts to KB/MB/GB) |

The state sensor additionally carries `samples` (all sensor counters),
`record_file`, `firmware`, `device_name`, `last_error`, `connect_count` and
`failed_attempts` as JSON attributes, so per-stream counters and errors don't
need their own entities.

Implementation notes:

* All values travel in **one** retained JSON document on
  `<prefix>/<device-id>/state`; each entity reads its key with a
  `value_template`. Null values are omitted and templates fall back to
  `unknown`, so a headband without battery info shows "unknown" rather than
  an error.
* Availability is `<prefix>/<device-id>/availability`: retained `online` on
  connect, retained `offline` on clean shutdown, and an LWT publishes
  `offline` if the container dies without disconnecting.
* State is published at most every `MUSE_MQTT_INTERVAL` seconds (default 10)
  and immediately whenever `state` or `connected` changes.
* Discovery configs are retained, republished when Home Assistant restarts
  (`homeassistant/status` → `online`) and when the firmware version becomes
  known (it lands in the device's `sw_version`).
* MQTT problems never take the supervisor down: connect failures are handled
  by paho's automatic reconnect, publish ticks are skipped while offline, and
  if `paho-mqtt` is not installed the error is logged once and the capture
  continues without MQTT.
* The dashboard page shows `mqtt: connected to <host>:<port>` / `mqtt:
  connecting to ...` / `mqtt: disabled`; the same line is in the `supervisor`
  object of `/status.json`.

## The mne-lsl 1.9.0 pin

mne-lsl 1.10+ ships no manylinux aarch64 wheels; 1.9.0 is the last release
with them (and bundles liblsl). OpenMuse only uses `StreamInfo`,
`StreamOutlet` and `local_clock`, which all exist in 1.9.0, so the pin costs
nothing here.

## Build and push

On the build machine (with qemu binfmt registered for aarch64):

```sh
./build.sh                    # build, tag = short git sha (-dirty if needed)
./build.sh --tag 1.0.0        # explicit tag
./build.sh --push             # push :<tag> and :latest (both architectures)
```

Do not run `build.sh --push` unless you mean it; the registry is
`registry.apps.nickv.me/nijave/openmuse`.

## Install on the Pine

1. Enable lingering so user units run without a login:

   ```sh
   sudo loginctl enable-linger "$USER"
   ```

2. Install the unit and the env file:

   ```sh
   mkdir -p ~/.config/containers/systemd
   cp openmuse-capture.container ~/.config/containers/systemd/
   cp openmuse-capture.env.example ~/.config/openmuse-capture.env
   chmod 600 ~/.config/openmuse-capture.env
   ```

   Edit `~/.config/openmuse-capture.env`: set `MUSE_ADDRESS` to the headband
   MAC (`00:55:DA:xx:xx:xx`), adjust `MUSE_PRESET`/`MUSE_ROTATE_AT` if
   needed.

3. Start it (the first start pulls the image from the registry, which takes
   a while on the Pine):

   ```sh
   systemctl --user daemon-reload
   systemctl --user start openmuse-capture
   systemctl --user status openmuse-capture
   journalctl --user -u openmuse-capture -f
   ```

## Dashboard

`http://<pine>:8088/` — state badge, child PID, full `status.json` (human
units), data dir listing. Refreshes every 5 s.

`http://<pine>:8088/healthz` — `200` while the child is alive and
`status.json` is less than 60 s old, else `503`.

## Notes

* **D-Bus socket**: bleak talks to BlueZ over the system D-Bus, so the unit
  mounts `/run/dbus/system_bus_socket` into the container. Fedora's BlueZ
  D-Bus policy allows any user to call BlueZ. Two settings make the socket
  usable from a rootless container (both verified on Fedora 44):
  `UserNS=keep-id` with `User=%U:%G`, because D-Bus EXTERNAL auth rejects a
  process whose claimed uid differs from the uid the bus sees on the socket,
  and `SecurityLabelDisable=true`, because SELinux denies `container_t` a
  write on the `system_dbusd_var_run_t` socket. The unit does not run as the
  image's own uid 10001; files in `~/muse-data` belong to the host user.
* **Network=host**: LSL outlet discovery is UDP multicast, which does not
  cross the rootless pasta network; host networking also publishes the
  dashboard port directly.
* **Data**: recordings and `status.json` live in `~/muse-data` on the host
  (`Volume=%h/muse-data:/data:Z`).
* **Auto-update**: `AutoUpdate=registry` with
  `systemctl --user enable --now podman-auto-update.timer`; the unit pins
  `:latest` and every push moves it.
