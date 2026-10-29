#!/usr/bin/with-contenv bashio
# ==============================================================================
# Alula BAT-Connect add-on entrypoint
# Reads /data/options.json (Supervisor-managed) and the MQTT service credentials
# Supervisor injects when services: mqtt:need is set.
# ==============================================================================
set -euo pipefail

# Expose MQTT credentials to the Python process (Supervisor-provided when the
# Mosquitto broker add-on / MQTT integration is available).
if bashio::services.available "mqtt"; then
  export MQTT_HOST="$(bashio::services "mqtt" "host")"
  export MQTT_PORT="$(bashio::services "mqtt" "port")"
  export MQTT_USERNAME="$(bashio::services "mqtt" "username")"
  export MQTT_PASSWORD="$(bashio::services "mqtt" "password")"
  bashio::log.info "Using Supervisor MQTT service at ${MQTT_HOST}:${MQTT_PORT}"
else
  # Fallback for local testing / edge cases
  export MQTT_HOST="${MQTT_HOST:-core-mosquitto}"
  export MQTT_PORT="${MQTT_PORT:-1883}"
  bashio::log.warning "MQTT service not advertised by Supervisor; using ${MQTT_HOST}:${MQTT_PORT}"
fi

export PYTHONPATH=/app
exec python3 -m alula_bridge
