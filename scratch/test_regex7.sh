#!/bin/bash
q="'/home/user/...' "

if [[ ! $q =~ /mnt/c/ ]]; then
  echo "Not matched"
else
  echo "Matched"
fi
