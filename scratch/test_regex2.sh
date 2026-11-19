#!/bin/bash
q="'/mnt/c/Users/ventu/Projects/local-ai/adb-{serial}.lock'"

# Try to match the quote at the start
if [[ $q =~ ^'/mnt/c/ ]]; then
  echo "Matches WSL"
else
  echo "Does not match WSL"
fi
