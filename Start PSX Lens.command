#!/bin/zsh
cd -- "$(dirname -- "$0")" || exit 1
printf 'Open http://127.0.0.1:8765 in your browser.\n'
python3 server.py
