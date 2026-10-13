#!/bin/bash
# Double-clic dans le Finder pour lancer l'application.
cd "$(dirname "$0")" || exit 1
exec python3 -m nas_move
