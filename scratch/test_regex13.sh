#!/bin/bash
q="'/mnt/c'"
if [[ $q =~ ^[\'\"]?/mnt/[c-z] ]]; then
    echo "Matches"
else
    echo "Does not match"
fi
