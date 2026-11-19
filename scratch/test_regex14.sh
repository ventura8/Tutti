#!/bin/bash
q="'/mnt/camera/'"
if [[ $q =~ ^[\'\"]?/mnt/[c-z] ]]; then
    echo "Matches"
else
    echo "Does not match"
fi
