#!/usr/bin/env bash
# apply-power-rules.sh: Apply required permissions for GPU/CPU power monitoring.
# Must be run as root or with sudo.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root"
   exit 1
fi

# Set capability for intel_gpu_top
if command -v intel_gpu_top >/dev/null; then
    setcap cap_perfmon+ep "$(command -v intel_gpu_top)"
    echo "Set capability for intel_gpu_top."
else
    echo "intel_gpu_top not found, skipping capability setting."
fi

# Create udev rule for intel-rapl
echo 'SUBSYSTEM=="powercap", ATTR{name}=="intel-rapl:0", MODE="0666"' > /etc/udev/rules.d/99-tutti-power.rules
udevadm control --reload-rules
udevadm trigger
echo "Applied udev rule for intel-rapl."
