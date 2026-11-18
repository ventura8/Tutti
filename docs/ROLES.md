# Tutti Role Assignment

Tutti automatically assigns roles to devices and machines based on their capabilities (detected via probes).

## Role Matrix

| Role | Description | Detection Criteria |
| :--- | :--- | :--- |
| `host` | Main host machine | Must be a machine |
| `worker` | CPU/GPU/local model runner | GPU, NPU, or Ollama detected |
| `test_runner` | Test executor | adb (device) or git/browser (machine) |
| `device_host` | adb/AirPlay/HA host | adb detected |
| `cast_receiver` | Cast/AirPlay target | TV detected |
| `presence_source` | Presence detection | Sensors/probes |
| `energy_meter` | Energy usage metering | Energy meter device |
| `notification_target` | Notifications | All devices |
| `dashboard_viewer` | Dashboard viewer | Browser |
| `storage_cache` | Storage/Cache | Disk space |
| `ci_runner` | CI jobs runner | git |

## Automatic Role Detection

Tutti performs daily checks to re-evaluate capabilities. Capabilities are detected via probes:
- Machine: GPU/NPU check, tool availability (git, adb, browser).
- Device: Android device serial, kinds (phone, tablet, TV, watch), loaded sensors.

## Remediating Roles

If a role is not assigned due to missing dependencies, Tutti provides one-click remediation in the dashboard to install required tools.

## Device-Specific Considerations

- **Watches**: Devices with `kind: watch` are treated as weak devices with stricter rules for battery (min 70%) and heat (max 35C), and are restricted from heavy roles like `worker`.
