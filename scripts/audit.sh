#!/usr/bin/env bash
# audit.sh: monitor CPU, GPU, and device utilization for a given PID.
PID=$1
interval=5

# Function to get host CPU load average
get_host_cpu_load() {
    awk '{print $1}' /proc/loadavg
}

# Function to get process CPU utilization (percentage)
get_proc_cpu_usage() {
    ps -p "$PID" -o %cpu= | tr -d ' '
}

# Function to get GPU utilization
get_gpu_usage() {
    if command -v nvidia-smi > /dev/null; then
        nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits | head -n 1
    else
        echo "0"
    fi
}

echo "AUDIT: Starting audit for PID=$PID"
while kill -0 "$PID" 2>/dev/null; do
    host_load=$(get_host_cpu_load)
    proc_cpu=$(get_proc_cpu_usage)
    gpu=$(get_gpu_usage)
    echo "AUDIT: HostLoad=$host_load ProcCPU=$proc_cpu% GPU=$gpu%"
    sleep "$interval"
done

