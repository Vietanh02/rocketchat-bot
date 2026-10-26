#!/usr/bin/env bash
# Example script command: free disk space on the bot's host, as a code block.
set -euo pipefail

echo '```'
df -h / | awk 'NR==1 || NR==2'
echo '```'
