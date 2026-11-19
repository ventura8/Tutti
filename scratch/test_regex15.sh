#!/bin/bash
q="'/mnt/camera/'"
if [[ $q =~ ^[\'\"]?/mnt/[c-z]/ ]]; then
    echo "Matches"
else
    echo "Does not match"
fi
q2="'/mnt/c/Users'"
if [[ $q2 =~ ^[\'\"]?/mnt/[c-z]/ ]]; then
    echo "Matches q2"
else
    echo "Does not match q2"
fi
