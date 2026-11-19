#!/bin/bash
q="'/mnt/c/Users/ventu/Projects/local-ai/adb-{serial}.lock'"

if [[ ! $q =~ "/mnt/c/" ]]; then
  echo "Not matched"
else
  echo "Matched"
fi
