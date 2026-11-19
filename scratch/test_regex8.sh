#!/bin/bash
q="'/mnt/c/Users/ventu/Projects/local-ai/adb-serial.lock'"
if [[ ! $q =~ /mnt/c/ ]]; then
    echo "Does not match"
else
    echo "Matches"
fi

p="/mnt/c/Users/ventu/Projects/local-ai/adb-serial.lock"
q2=$(printf '%q' "$p")
echo "q2: $q2"
if [[ ! $q2 =~ /mnt/c/ ]]; then
    echo "Does not match q2"
else
    echo "Matches q2"
fi
