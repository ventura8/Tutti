#!/bin/bash
q="'/mnt/c/Users/ventu/Projects/local-ai/adb-{serial}.lock'"
# q is already quoted by shlex.quote

if ! [[ $q =~ ^.\'/mnt/c/ ]]; then
  echo "Not a WSL path"
else
  echo "WSL path"
fi

q="'/home/user/lock.lock'"
if ! [[ $q =~ ^.\'/mnt/c/ ]]; then
  echo "Not a WSL path"
else
  echo "WSL path"
fi
