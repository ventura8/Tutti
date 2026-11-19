#!/bin/bash
q="'/mnt/c/Users/ventu/Projects/local-ai/adb-serial.lock'"
if [[ $q =~ ^[\'\"]?/mnt/ ]]; then
    echo "Matches"
else
    echo "Does not match"
fi
