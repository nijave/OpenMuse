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
  `pip install --no-deps` plus `bleak`, `numpy`, `pandas`, `mne-lsl==1.9.0`.
  Runs as the non-root user `openmuse` (UID 10001).
* `dashboard.py` — supervisor + status dashboard (stdlib only); after each
  rotation (and at startup, for leftovers from a previous run) it compresses
  finished record files in place with `zstd`, so an overnight capture ends up
  as `muse_*.txt.zst` (roughly 2.5x smaller; the live file stays uncompressed
  for crash safety).
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

Finished (rotated or leftover) `muse_*.txt` recordings are compressed to
`muse_*.txt.zst` a minute or so after the child stops writing them; the file
the live child is writing is never touched. Compression activity shows on the
dashboard page and under `supervisor` in `/status.json` (`compressing` and
`compress`). Read the `.zst` files back with
`OpenMuse.read_rawdata_lines("muse_....txt.zst")`.

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
