# Hosts

Tutti runs on Linux, macOS, WSL, Windows and single-board computers such as Raspberry Pi.

## Raspberry Pi

A Raspberry Pi (arm64 or armhf, Raspberry Pi OS or Ubuntu) can act as a Tutti host for light and always-on roles:

- **Coordinator / dashboard** (LAN or VPN only, never public)
- **ADB device hub** for wired or wireless Android test devices
- **Presence / sensor probe** (temperature and throttling via `vcgencmd`)
- **Light checks runner** (lint, unit tests, small scripts)
- **Small local model** (e.g. a compact llama model via Ollama on 8–16 GB Pi 5)

### Setup

1. Install system tools on the Pi:
   ```bash
   sudo apt update
   sudo apt install -y python3-venv git adb rsync
   ```
   On Raspberry Pi OS, `vcgencmd` is preinstalled (add your user to the `video` group if needed).

2. Install Tutti at user level (no root required):
   ```bash
   git clone https://github.com/ventura8/Tutti
   cd Tutti
   bash install/install.sh
   ```

3. Add the host to `tutti.config.yaml` with conservative slots and heat caps:
   ```yaml
   machines:
     rpi:
       host: rpi.example   # ssh alias in ~/.ssh/config
       slots: 1
       min_free_gb: 2
       heat:
         cap_c: 75
         stop_c: 82
   ```

4. When `vcgencmd` is on `PATH`, Tutti's probe automatically reads CPU temperature (`vcgencmd measure_temp`) and throttling status (`vcgencmd get_throttled`).
