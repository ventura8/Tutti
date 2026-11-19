#!/bin/bash
q="/mnt/c/Users/ventu/Projects/local-ai/adb-serial.lock"
echo "Testing with path: $q"
if timeout 5 test -d "$(dirname -- "$q")" 2>/dev/null; then
  echo "Directory exists"
else
  echo "Directory does not exist"
fi
