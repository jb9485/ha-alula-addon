#!/usr/bin/with-contenv bashio
# ==============================================================================
# Alula BAT-Connect add-on entrypoint
# Reads /data/options.json (Supervisor-managed) and the MQTT service credentials
# Supervisor provides when services: mqtt:need is set.
#
# The Mosquitto add-on can register its MQTT service with Supervisor a little
# after this add-on starts (e.g. right after a restore or a host reboot). Never
# fall back to an anonymous connection: wait for the service, and if it never
# appears exit non-zero so the Supervisor watchdog restarts the add-on.
# ==============================================================================
set -euo pipefail

MQTT_WAIT_SECONDS="${MQTT_WAIT_SECONDS:-300}"
MQTT_WAIT_STEP=5

mqtt_service_ready() {
  bashio::services.available "mqtt" || return 1
  MQTT_HOST="$(bashio::services "mqtt" "host")"
  MQTT_PORT="$(bashio::services "mqtt" "port")"
  MQTT_USERNAME="$(bashio::services "mqtt" "username")"
  MQTT_PASSWORD="$(bashio::services "mqtt" "password")"
  [[ -n "${MQTT_HOST}" && -n "${MQTT_USERNAME}" && -n "${MQTT_PASSWORD}" ]]
}

if [[ -n "${MQTT_HOST:-}" && -n "${MQTT_USERNAME:-}" ]]; then
  # Explicit broker settings from the environment (local testing only).
  export MQTT_HOST MQTT_USERNAME MQTT_PASSWORD="${MQTT_PASSWORD:-}"
  export MQTT_PORT="${MQTT_PORT:-1883}"
  bashio::log.info "Using MQTT broker from the environment at ${MQTT_HOST}:${MQTT_PORT}"
else
  waited=0
  until mqtt_service_ready; do
    if (( waited >= MQTT_WAIT_SECONDS )); then
      bashio::log.error "MQTT service still not available from Supervisor after ${waited}s." \
        "Is the Mosquitto broker add-on running? Exiting so the add-on is restarted."
      exit 1
    fi
    if (( waited == 0 )); then
      bashio::log.warning "MQTT service not available from Supervisor yet; waiting" \
        "(up to ${MQTT_WAIT_SECONDS}s)"
    fi
    sleep "${MQTT_WAIT_STEP}"
    waited=$(( waited + MQTT_WAIT_STEP ))
    bashio::cache.flush_all || true
  done
  export MQTT_HOST MQTT_PORT MQTT_USERNAME MQTT_PASSWORD
  bashio::log.info "Using Supervisor MQTT service at ${MQTT_HOST}:${MQTT_PORT}" \
    "(after ${waited}s)"
fi

export PYTHONPATH=/app
exec python3 -m alula_bridge
