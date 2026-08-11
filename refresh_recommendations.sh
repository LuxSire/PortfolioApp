#!/bin/bash
# refresh_recommendations.sh — regenerates data/recommendations.json and
# syncs it (along with whatever else in data/ changed) to web/public/, so
# the running Vite dev server actually serves the refreshed file -- the
# frontend fetches /recommendations.json from web/public/, a static copy,
# not data/ directly (see web/package.json's sync-data script).
#
# Zero network calls, zero IB Gateway connection -- see recommendations.py's
# own docstring, same "just recompute from files already on disk" class of
# operation as `main.py rescore`. Safe to run on an unattended timer: no
# clientId collision risk with ib_price_server.py's own IB Gateway
# connection, unlike main.py modes that fetch fresh data (all/prices/
# form4/13f/themes).
#
# Invoked hourly, 16:00-22:00 CET/CEST, via a launchd LaunchAgent (see
# ~/Library/LaunchAgents/com.ibkrpe.recommendations-refresh.plist) --
# launchd runs jobs with a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin,
# no Homebrew), which made `npm run sync-data` fail with "env: node: No
# such file or directory" the first time this ran (npm itself was called
# via absolute path, but npm's own shebang looks up `node` on PATH) --
# export a real PATH explicitly here rather than depending on the
# caller's environment.
export PATH="/opt/homebrew/bin:$PATH"
set -e
cd "$(dirname "$0")"
.venv/bin/python3 main.py recommendations
cd web
npm run sync-data
