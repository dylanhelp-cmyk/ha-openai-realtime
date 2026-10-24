#!/usr/bin/with-contenv bashio
set -e
# Python reads /data/options.json directly; no key/token logging or shell exports.
exec python3 -m app.main
