"""Persists WiFi credentials to a JSON file on the device's flash, so they
can be set at runtime (via serial_provisioning.py + the companion app)
instead of being hardcoded in config.py.
"""

import os

import ujson

WIFI_FILE = "/wifi.json"


def load():
    """Returns (ssid, password), or (None, None) if nothing has been saved."""
    try:
        with open(WIFI_FILE) as f:
            data = ujson.load(f)
        return data.get("ssid"), data.get("password")
    except (OSError, ValueError):
        return None, None


def save(ssid, password):
    with open(WIFI_FILE, "w") as f:
        ujson.dump({"ssid": ssid, "password": password}, f)


def clear():
    """Deletes the saved credentials file, if any. Returns True if a file
    was actually removed, False if there was nothing saved."""
    try:
        os.remove(WIFI_FILE)
        return True
    except OSError:
        return False


def get_credentials(default_ssid, default_password):
    """Returns saved credentials if present, else the given config.py defaults."""
    ssid, password = load()
    if ssid:
        return ssid, password
    return default_ssid, default_password
