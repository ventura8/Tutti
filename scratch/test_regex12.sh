#!/bin/bash
q="'/mnt/c/Users/ventu/Projects/local-ai/adb-serial.lock'"
if [[ $q =~ ^[\'\"]?/mnt/[c-z]/ ]]; then
    echo "Matches"
else
    echo "Does not match"
fi
q2="'/mnt/usb/lock'"
if [[ $q2 =~ ^[\'\"]?/mnt/[c-z]/ ]]; then
    echo "Matches q2"
else
    echo "Does not match q2"
fi
