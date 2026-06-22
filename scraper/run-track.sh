#!/bin/bash
#
# Wrapper script to run track.py with environment variables loaded
#
# Usage: ./run-track.sh [arguments for track.py]
# Example: ./run-track.sh -a myaccount
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load .env file if it exists
if [ -f "${SCRIPT_DIR}/.env" ]; then
    export $(grep -v '^#' "${SCRIPT_DIR}/.env" | xargs)
fi

# Run track.py with all arguments passed through
python3 "${SCRIPT_DIR}/track.py" "$@"
