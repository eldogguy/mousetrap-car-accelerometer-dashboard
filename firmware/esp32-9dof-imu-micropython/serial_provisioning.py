"""Non-blocking JSON-over-USB-serial command listener, so a desktop
companion app can configure WiFi credentials while the device is plugged
in -- without interrupting the sampling loop in main.py.

Wire protocol: one JSON object per line, newline-terminated, in both
directions. Every reply this device sends is prefixed with RESPONSE_PREFIX
so a host app can tell protocol replies apart from the plain `print()`
debug logging main.py also writes to the same serial port.

Commands (host -> device):
  {"cmd": "ping"}
  {"cmd": "wifi_get"}
  {"cmd": "wifi_set", "ssid": "...", "password": "..."}
  {"cmd": "wifi_clear"}                     -- deletes the saved credentials
  {"cmd": "wifi_test"}                      -- attempts to connect now
  {"cmd": "wifi_scan"}                      -- scans for nearby networks
  {"cmd": "dump_csv"}                       -- prints the current sample buffer as CSV

Replies (device -> host), always prefixed with RESPONSE_PREFIX:
  {"ok": true, "pong": "esp32-9dof-imu", "version": "1.0"}
  {"ok": true, "ssid": "..."}               -- or "ssid": null if unset
  {"ok": true}                              -- wifi_set succeeded
  {"ok": true, "cleared": true}             -- wifi_clear succeeded (false if nothing was saved)
  {"ok": true, "ip": "192.168.1.42"}        -- wifi_test succeeded
  {"ok": true, "networks": [{"ssid": "...", "rssi": -52, "channel": 6, "secure": true}, ...]}
  {"ok": false, "error": "..."}

dump_csv doesn't reply with the usual @IMU@ JSON line -- the CSV body is
multi-line, so instead it prints CSV_DUMP_BEGIN, then the raw CSV (header +
rows, unprefixed), then CSV_DUMP_END, then a final @IMU@ status reply with
the sample count. This intentionally reads the *existing* buffer over the
already-open serial connection -- unlike reconnecting fresh (e.g. a new
mpremote session), it doesn't reset the board or disturb a running/armed
recording.

Note: wifi_scan blocks the main loop for a few seconds while the radio
scans -- avoid triggering it while an experiment is actively recording.
"""

import sys

import network
import ujson
import utime

try:
    import uselect as _select
except ImportError:
    import select as _select

import wifi_store
from config import WIFI_SSID, WIFI_PASSWORD, WIFI_CONNECT_TIMEOUT_MS

RESPONSE_PREFIX = "@IMU@"
CSV_DUMP_BEGIN = "@IMU_CSV_BEGIN@"
CSV_DUMP_END = "@IMU_CSV_END@"

_poll = _select.poll()
_poll.register(sys.stdin, _select.POLLIN)

_csv_provider = None  # set by main.py: (get_header, get_sample_count, get_row)


def set_csv_provider(get_header, get_sample_count, get_row):
    global _csv_provider
    _csv_provider = (get_header, get_sample_count, get_row)


def _reply(obj):
    print(RESPONSE_PREFIX + ujson.dumps(obj))


def _handle_command(line):
    try:
        msg = ujson.loads(line)
    except ValueError:
        return  # not a protocol line (e.g. someone typing at the REPL); ignore

    cmd = msg.get("cmd")

    if cmd == "ping":
        _reply({"ok": True, "pong": "esp32-9dof-imu", "version": "1.0"})

    elif cmd == "wifi_get":
        ssid, _password = wifi_store.load()
        _reply({"ok": True, "ssid": ssid})

    elif cmd == "wifi_set":
        ssid = msg.get("ssid") or ""
        password = msg.get("password") or ""
        if not ssid:
            _reply({"ok": False, "error": "ssid required"})
            return
        wifi_store.save(ssid, password)
        _reply({"ok": True})

    elif cmd == "wifi_clear":
        cleared = wifi_store.clear()
        _reply({"ok": True, "cleared": cleared})

    elif cmd == "wifi_test":
        ssid, password = wifi_store.get_credentials(WIFI_SSID, WIFI_PASSWORD)
        if not ssid:
            _reply({"ok": False, "error": "no ssid configured"})
            return
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        if not wlan.isconnected():
            wlan.connect(ssid, password)
            start = utime.ticks_ms()
            while not wlan.isconnected():
                if utime.ticks_diff(utime.ticks_ms(), start) > WIFI_CONNECT_TIMEOUT_MS:
                    _reply({"ok": False, "error": "timed out connecting to %s" % ssid})
                    return
                utime.sleep_ms(200)
        _reply({"ok": True, "ip": wlan.ifconfig()[0]})

    elif cmd == "wifi_scan":
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        try:
            results = wlan.scan()
        except OSError as e:
            _reply({"ok": False, "error": "scan failed: %s" % e})
            return

        seen = set()
        networks = []
        for ssid_bytes, _bssid, channel, rssi, authmode, _hidden in results:
            try:
                ssid = ssid_bytes.decode("utf-8")
            except Exception:
                continue
            if not ssid or ssid in seen:
                continue  # skip blank/hidden SSIDs, de-dupe APs seen on multiple channels
            seen.add(ssid)
            networks.append({
                "ssid": ssid,
                "rssi": rssi,
                "channel": channel,
                "secure": authmode != 0,
            })
        networks.sort(key=lambda n: n["rssi"], reverse=True)
        _reply({"ok": True, "networks": networks})

    elif cmd == "dump_csv":
        if _csv_provider is None:
            _reply({"ok": False, "error": "csv provider not registered"})
            return
        get_header, get_sample_count, get_row = _csv_provider
        n = get_sample_count()
        print(CSV_DUMP_BEGIN)
        print(get_header(), end="")
        for i in range(n):
            print(get_row(i), end="")
        print(CSV_DUMP_END)
        _reply({"ok": True, "sample_count": n})

    else:
        _reply({"ok": False, "error": "unknown cmd"})


def poll():
    """Call once per main-loop iteration. Non-blocking: handles at most one
    fully-received command line per call, does nothing if none is waiting."""
    if _poll.poll(0):
        line = sys.stdin.readline()
        if line:
            _handle_command(line.strip())
