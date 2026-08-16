#!/bin/bash
# Run the PocketTUI tmux terminal backend (port 5560).
cd "$(dirname "$0")"
exec micromamba run -n pockettui python app.py --port 5560 "$@"
