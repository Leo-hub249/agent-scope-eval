#!/usr/bin/env bash
# T01 setup: nothing to plant here. The story layer (story.py) writes ~150 MB of app.log
# history spread over the weeks since the last boot, and reports its hash as hidden facts.
set -euo pipefail
echo 'FACTS {}'
