#!/usr/bin/with-contenv bashio

# Read configuration from add-on options
GPIO_PIN=$(bashio::config 'gpio_pin')

bashio::log.info "Pi-Somfy Add-on starting..."
bashio::log.info "GPIO pin: ${GPIO_PIN}"

# Ensure the persistent config directory exists
CONFIG_FILE="/data/operateShutters.conf"
if [ ! -f "${CONFIG_FILE}" ]; then
    bashio::log.info "No existing config found, creating from defaults..."
    cp /somfy/defaultConfig.conf "${CONFIG_FILE}"
fi

# Update the TXGPIO setting in the config file
if grep -q "^TXGPIO" "${CONFIG_FILE}"; then
    sed -i "s/^TXGPIO.*/TXGPIO = ${GPIO_PIN}/" "${CONFIG_FILE}"
else
    sed -i "/^\[General\]/a TXGPIO = ${GPIO_PIN}" "${CONFIG_FILE}"
fi

# Ensure log location exists and is writable
sed -i "s|^LogLocation.*|LogLocation = /data/|" "${CONFIG_FILE}"

# Detect Pi model — Pi 5 uses lgpio (no daemon), older Pis use pigpio (needs pigpiod)
# /proc/device-tree/model may not be accessible inside the container; fall back to
# checking for /dev/gpiochip4 (RP1 chip, Pi 5 only) or the CPU revision code.
IS_PI5=false
PI_MODEL="unknown"
if [ -f /proc/device-tree/model ]; then
    PI_MODEL=$(tr -d '\0' < /proc/device-tree/model)
fi
bashio::log.info "Detected board: ${PI_MODEL}"

# Log available gpiochip devices for diagnostics
bashio::log.info "Available gpiochip devices: $(ls /dev/gpiochip* 2>/dev/null || echo 'none')"

if echo "${PI_MODEL}" | grep -q "Pi 5"; then
    IS_PI5=true
elif [ -e /dev/gpiochip4 ]; then
    bashio::log.info "/dev/gpiochip4 found — assuming Pi 5"
    IS_PI5=true
elif grep -q "^Revision.*[[:space:]].*[cd]0[34]17" /proc/cpuinfo 2>/dev/null; then
    bashio::log.info "Pi 5 CPU revision detected in /proc/cpuinfo"
    IS_PI5=true
fi

if [ "${IS_PI5}" = true ]; then
    bashio::log.info "Pi 5 detected — using lgpio (no pigpiod needed)"
else
    bashio::log.info "Starting pigpiod..."
    pigpiod -l -m
    sleep 1

    if ! pgrep -x pigpiod > /dev/null; then
        bashio::log.error "Failed to start pigpiod!"
        exit 1
    fi

    bashio::log.info "pigpiod started successfully"
fi

# Launch Pi-Somfy with web interface only (no MQTT, no Alexa)
cd /somfy
bashio::log.info "Starting Pi-Somfy..."
exec python3 operateShutters.py -c "${CONFIG_FILE}" -a
