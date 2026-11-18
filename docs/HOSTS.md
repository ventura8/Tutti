# Hosts

Tutti is designed to run on a variety of hosts.

## Raspberry Pi

Raspberry Pi (arm64/armhf, Raspberry Pi OS/Ubuntu) is supported as a Tutti host.

### Setup

1. Install required dependencies on the Pi:
   ```bash
   sudo apt update
   sudo apt install -y python3-pip python3-venv git adb rsync vcgencmd
   ```

2. Add the host to `tutti.config.yaml`:
   ```yaml
   machines:
     rpi:
       host: rpi  # ssh alias in ~/.ssh/config
       slots: 1
       min_free_gb: 2
   ```

3. Ensure the Raspberry Pi's `vcgencmd` is in the `PATH` of the user running tutti. Tutti will automatically probe temperature and throttling.
