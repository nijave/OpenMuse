# OpenMuse

*The Open Python Muse S Athena EEG Decoder*

[![CI](https://github.com/DominiqueMakowski/OpenMuse/actions/workflows/ci.yml/badge.svg)](https://github.com/DominiqueMakowski/OpenMuse/actions/workflows/ci.yml)
[![Muse S Athena](media/device.webp)](https://eu.choosemuse.com/products/muse-s-athena)


This software allows recording, streaming via LSL and visualizing signals from the **Muse S Athena** headband.

> [!WARNING]  
> OpenMuse is NOT an official product of InteraXon Inc and is not affiliated with or endorsed by the company. It does not come with any warranty and should be considered an experimental software developed for research purposes only.

## Roadmap

- [x] Record raw data to a file
- [x] Decode raw data into a pandas DataFrame
- [x] Stream data over LSL
- [x] Visualise live LSL streams
- [x] Stream in battery signal and display battery level in visualizer
- [ ] Consider adding a visualization of the PSD of each signal
- [ ] Validate timestamping accuracy experimentally: confirm we get time-locked ERPs
- [ ] Validate the best PPG signal extraction method
- [ ] Validate fNIRS signal extraction method

## Installation

Install from GitHub by opening a terminal and running:

```powershell
pip install https://github.com/DominiqueMakowski/OpenMuse/zipball/main
```

## Usage

After installing the package, open a terminal and use the following commands:

### Find Muse devices

Power up the Muse S Athena headband (a blue light should appear on the front) and run:

```powershell
OpenMuse find
```

This will print the MAC addresses of nearby Muse devices. Note the address of your device for the next steps.


### Record data to a file

```powershell
OpenMuse record --address <your-muse-address> --duration 60 --outfile data.txt
```

> [!TIP]
> By default, recording and streaming use the `--preset p1041`, which enables all channels. You can change the preset using the `--preset` argument (see below for the list of documented presets).
>
> Run `OpenMuse presets` to print a table of the catalogued presets (`--model legacy` or `--model all` for older headbands, `--describe p1041` for one preset's details). An unrecognized `--preset` string is still sent to the device unchanged, with a warning.

#### Loading Recorded Data

Once your file is recorded, you can load it in Python using:

```python
import pandas as pd
import OpenMuse 

with open("data.txt", "r", encoding="utf-8") as f:
    messages = f.readlines()
data = OpenMuse.decode_rawdata(messages)

# Plot Movement Data
data["ACCGYRO"].plot(
    x="time",
    y=["ACC_X", "ACC_Y", "ACC_Z", "GYRO_X", "GYRO_Y", "GYRO_Z"],
    subplots=True
)
```

![](./media/example_accgyro.png)

### Lab Streaming Layer (LSL)

> [!NOTE]  
> OpenMuse uses [MNE-LSL](https://mne.tools/mne-lsl/), an improved python-binding for the Lab Streaming Layer C++ library, `mne_lsl.lsl`, replacing `pylsl`.

To stream data over LSL, use:

```powershell
OpenMuse stream --address <your-muse-address>
```

This creates **separate LSL streams** for groups of channels (`Muse_EEG`,  `Muse_ACCGYRO`, etc.).

The following options are available:
- `--address`: The MAC address of your Muse device (required)
- `--duration`: Duration of the recording in seconds (if not specified, streaming will continue until you press Ctrl+C in the terminal)
- `--preset`: Preset configuration (default: `p1041` for all channels)
- `--record`: Record raw BLE packets to a file (optional)
- `--auto-reconnect`: Automatically reconnect and resume streaming if the BLE link drops (recommended for long/overnight recordings); LSL outlets are reused so downstream sees one continuous stream
- `--reconnect-attempts` / `--reconnect-delay`: Maximum consecutive failed reconnect attempts (default 5) and seconds between attempts (default 3.0); the attempt counter resets after any 30 s of successful streaming
- `--status-file PATH`: Write a machine-readable JSON status file for external dashboards (process state, connection status, battery, sample counts); with multiple devices, one file per device is written with the address inserted before the extension
- `--status-interval SECONDS`: Minimum seconds between periodic status file updates while running (default 5.0); state transitions are always written immediately

#### Status file

When `--status-file` is given, `OpenMuse stream` continuously writes a JSON file describing the streaming process, so an external dashboard (or script) can tell whether the process is running, connected to the headband, and still receiving data. The file is written atomically on every state transition and at least every `--status-interval` seconds:

```json
{
  "version": 1,
  "updated": 1762450000.123,
  "address": "00:55:DA:B9:FA:20",
  "preset": "p1041",
  "state": "streaming",
  "connected": true,
  "device_name": "Muse-A8A1",
  "firmware": "10.92.0",
  "battery_percent": 87.5,
  "battery_time_to_empty_s": 12600.0,
  "samples": {"EEG": 256000, "ACCGYRO": 52000, "OPTICS": 64000, "BATTERY": 1000, "DRL": 32000},
  "last_data_age_s": 0.1,
  "connected_since": 1762449900.0,
  "process_started": 1762449890.0,
  "connect_count": 1,
  "failed_attempts": 0,
  "last_error": null,
  "record_file": null,
  "record_bytes": null
}
```

`state` is one of `connecting` (trying to connect), `streaming` (connected and receiving data), `stalled` (connected but no data for more than 5 s), `reconnecting` (connection lost, retrying), `stopped` (clean exit after the duration elapsed or Ctrl+C), or `failed` (reconnect gave up, or an error ended the stream). Every field is always present; unknown values (e.g. battery before the first BATTERY packet) are `null`.

### Visualize Live Streams

To visualize live LSL streams, open a new terminal (while the streaming is running) and run:

```powershell
OpenMuse view
```

### Hyperscanning (Multiple Devices Streaming)

To record from multiple Muse devices simultaneously (e.g., for hyperscanning studies):

1. **Discover devices**: Power on all Muse headbands and run `OpenMuse find` to note down each device's MAC address.

2. **Start streaming**: You can stream from multiple devices simultaneously by passing multiple MAC addresses:

```powershell
OpenMuse stream --address 00:55:DA:B9:FA:20 00:55:DA:BB:CD:CD
```

This will manage all connections in parallel within a single process. LSL streams will be named with their respective device IDs (e.g., `Muse-EEG (MAC1)`), allowing you to distinguish them on the receiving end.

3. **Visualize (optional)**: When multiple devices are streaming, the viewer will automatically detect and display streams from only the first device by default to avoid confusion. To view a specific device, use the `--address` filter:

```powershell
# View first device
OpenMuse view --address 00:55:DA:B9:FA:20

# View second device (open in a separate terminal)
OpenMuse view --address 00:55:DA:BB:CD:CD
```

> [!NOTE]
> If you run `OpenMuse view` without the `--address` argument when multiple devices are streaming, it will automatically select the first detected device and display a warning message listing all available devices.

4. **Record with LabRecorder**: Use [LabRecorder](https://github.com/labstreaminglayer/App-LabRecorder) to record all streams to an XDF file. Select all the streams from your devices (e.g., `Muse-EEG (00:55:DA:B9:FA:20)`, `Muse-EEG (00:55:DA:BB:CD:CD)`, etc.).

#### Multi-Device Raw Recording (Alternative to LSL)

You can also do raw BLE packet recordings from multiple Muse devices simultaneously by passing multiple MAC addresses:

```powershell
OpenMuse record --address 00:55:DA:B9:FA:20 00:55:DA:BB:CD:CD --duration 60 --outfile data.txt
```

When recording from multiple devices, the device address will be automatically appended to the filename to avoid collisions (e.g., `data_0055DAB9FA20.txt`, `data_0055DABBCDCD.txt`).

## Technical Details

### Specs

Muse S Athena specs (From the [Muse website](https://eu.choosemuse.com/products/muse-s-athena)):
- Wireless Connection: BLE 5.3, 2.4 GHz
- EEG Channels: 4 EEG channels (TP9, AF7, AF8, TP10) + 4 amplified Aux channels
  - Sample Rate: 256 Hz
  - Sample Resolution: 14 bits / sample
- Accelerometer: Three-axis at 52Hz, 16-bit resolution, range +/- 2G
- Gyroscope: Three-axis at 52Hz, 16-bit resolution, range +/- 250dps
- PPG Sensor: Triple wavelength: IR (850nm), Near-IR (730nm), Red (660nm), 64 Hz sample rate, 20-bit resolution
- fNIRS Sensor: 5-optode bilateral frontal cortex hemodynamics, 64 Hz sample rate, 20-bit resolution

### Presets

| Presets                                 | EEG   | Optics   | ACC/GYRO | Battery | Red LED |
|-----------------------------------------|:-----:|:--------:|:--------:|:-------:|:--------|
| p20, p21, p50, p51, p60, p61            | EEG4  |   —      |    X     |    X    |   off   |
| p1034, p1043                            | EEG8  | Optics8  |    X     |    X    | bright  |
| p1044                                   | EEG8  | Optics8  |    X     |    X    |  dim    |
| p1035                                   | EEG4  | Optics4  |    X     |    X    |  dim    |
| p1041, p1042                            | EEG8  | Optics16 |    X     |    X    | bright  |
| p1045                                   | EEG8  | Optics4  |    X     |    X    |  dim    |
| p1046                                   | EEG8  | Optics4  |    X     |    X    |   —     |

*Table derived from the signature of the data packets present in the data. More presets could exist.*

*No `p4129` preset exists: the headband reports its current preset as the hex id in decimal (`"ps":4129` is `0x1021`, i.e. `p1021`), and the firmware rejects the literal string `p4129`. The fixture `tests/test_data/data_p4129.txt` holds whatever preset the device already had. Run `OpenMuse presets` for the full catalogue.*

### Acknowledgements

This project would not have been possible without the breakthroughs of [amused-Py](https://github.com/Amused-EEG/amused-py) that identified the communication protocol and [AbosaSzakal's parser](https://github.com/AbosaSzakal/MuseAthenaDataformatParser) who documented the data structure.
