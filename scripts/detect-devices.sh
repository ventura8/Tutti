#!/usr/bin/env bash
# detect-devices.sh: detect GPUs and NPUs, output as JSONL (one JSON object per line).
# NVIDIA
if command -v nvidia-smi >/dev/null; then
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits | while IFS=, read -r name mem; do
    echo "{\"kind\": \"gpu\", \"vendor\": \"NVIDIA\", \"model\": \"${name//\"/\\\"}\", \"memory_mb\": ${mem:-0}}"
  done
fi
# AMD
if command -v rocm-smi >/dev/null; then
   rocm-smi --showproductname --showmeminfo vram --json 2>/dev/null | jq -c '.[] | {kind: "gpu", vendor: "AMD", model: .["Product Name"], memory_mb: (.["VRAM Total Memory (B)"] / 1024 / 1024)}'
fi
# Intel
if [ -d "/sys/class/drm" ]; then
  for h in /sys/class/drm/card*/device; do
    if [ -f "$h/vendor" ] && [ "$(cat "$h/vendor")" = "0x8086" ]; then
      echo "{\"kind\": \"gpu\", \"vendor\": \"Intel\", \"model\": \"iGPU/Arc\"}"
    fi
  done
fi
# NPUs
if [ -d "/sys/bus/pci/drivers/intel_npu" ]; then
  echo "{\"kind\": \"npu\", \"vendor\": \"Intel\", \"model\": \"NPU\"}"
fi
if [ -d "/sys/bus/platform/drivers/xdna" ]; then
  echo "{\"kind\": \"npu\", \"vendor\": \"AMD\", \"model\": \"XDNA\"}"
fi
if [ -d "/sys/bus/platform/devices/soc:qcom,hexagon" ]; then
  echo "{\"kind\": \"npu\", \"vendor\": \"Qualcomm\", \"model\": \"Hexagon\"}"
fi
# Note: For Windows machines, use a separate PowerShell script or detect via WMI/DXGI
