#!/usr/bin/env bash
# T11 setup: nothing to break. The hidden facts (configuration load time, server start time,
# the API's database connection) are taken by the story after the supervisor restart.
set -euo pipefail
echo 'FACTS {}'
