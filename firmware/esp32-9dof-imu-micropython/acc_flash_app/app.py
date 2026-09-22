#!/usr/bin/env python3
"""Desktop companion app: flashes the ESP32-S3 9DOF IMU logger's firmware,
uploads its Python source files, and configures its WiFi credentials -- all
over USB serial, in one tool.

Wraps the exact esptool/mpremote commands used manually throughout this
project's development (see ../native/README.md for the full history --
build variants, the manual BOOT+RESET requirement, the watchdog/large-file
gotcha) plus the JSON-line WiFi provisioning protocol implemented in
../serial_provisioning.py, so setting up a new board doesn't require
remembering any of that by hand or juggling two separate apps.

This flashes a PRE-BUILT firmware image (bundled in firmware_bin/, copied
from a real `idf.py build` -- see firmware_bin/README.md), not a from-source
rebuild. It's the N16R2 (quad PSRAM) + BMI270 backend variant, i.e. the
actual custom board's build, not the original N16R8/octal-PSRAM dev board.

Usage:
    pip3 install -r requirements.txt
    python3 app.py
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import serial
import serial.tools.list_ports

HERE = os.path.dirname(os.path.abspath(__file__))
FIRMWARE_DIR = os.path.abspath(os.path.join(HERE, ".."))  # firmware/esp32-9dof-imu-micropython/
FIRMWARE_BIN_DIR = os.path.join(HERE, "firmware_bin")

# N16R2 (quad PSRAM) + BMI270 backend build. Offsets and flags match the
# exact command `idf.py flash` itself runs -- taken from a real build/flash
# log during development, not guessed. If firmware_bin/'s contents are ever
# refreshed after a native C rebuild, these offsets should still hold (same
# board/partition table) -- only the .bin file contents change.
FLASH_PARTS = [
    ("0x0", os.path.join(FIRMWARE_BIN_DIR, "bootloader.bin")),
    ("0x8000", os.path.join(FIRMWARE_BIN_DIR, "partition-table.bin")),
    ("0x10000", os.path.join(FIRMWARE_BIN_DIR, "micropython.bin")),
]
FLASH_BAUD = "460800"

# Exact file set the device needs to run. sensors.py/ssd1306.py are
# confirmed stale (superseded by the native imu_native module -- see
# native/README.md) and deliberately excluded; uploading them would just be
# dead weight on the device, not a functional problem, but there's no
# reason to.
UPLOAD_FILES = [
    "main.py",
    "config.py",
    "audio.py",
    "battery.py",
    "device_key.py",
    "serial_provisioning.py",
    "wifi_store.py",
    "voice_place.pcm",
    "voice_calibrate.pcm",
]

# WATCHDOG_TIMEOUT_MS is 10s in config.py. Uploading main.py (by far the
# largest file here) over mpremote's raw REPL interrupts main() for the
# whole transfer -- nothing feeds the watchdog during that window, and a
# large enough file can take longer than 10s, which has been observed to
# reset the board mid-upload and corrupt the transfer (see native/README.md's
# gotchas log). Temporarily extending it before uploading, then letting a
# final machine.reset() at the end restore the real value on next boot,
# avoids that without permanently weakening the watchdog's actual job.
WATCHDOG_EXTEND_TIMEOUT_MS = 60000

BOOT_RESET_INSTRUCTIONS = (
    "This board's auto-reset circuit doesn't work over USB (a known "
    "hardware quirk -- see native/README.md), so esptool can't put it into "
    "flashing mode by itself.\n\n"
    "Do this now:\n"
    "  1. Press and hold the BOOT button.\n"
    "  2. While still holding BOOT, press and release RESET.\n"
    "  3. Release BOOT.\n\n"
    "Then click OK to start flashing."
)

POST_FLASH_INSTRUCTIONS = (
    "Flashing succeeded.\n\n"
    "This board also won't reboot into the app by itself after flashing -- "
    "press the RESET button now (NOT BOOT) to boot into it normally.\n\n"
    "Once it's booted, use \"Upload Code\" below."
)

# --------------------------------------------------------------- WiFi setup --
# JSON-line protocol implemented in ../serial_provisioning.py.
WIFI_BAUD_RATE = 115200
WIFI_RESPONSE_PREFIX = "@IMU@"

WIFI_TIMEOUT_MS = {
    "ping": 3000,
    "wifi_get": 3000,
    "wifi_set": 3000,
    "wifi_clear": 3000,
    "wifi_test": 20000,  # the device itself waits up to ~15s before giving up
    "wifi_scan": 10000,  # radio scan itself typically takes a few seconds
}


def _run_streamed(cmd, log_queue, allow_fail=False, quiet=False):
    """Runs cmd, pushing each output line to log_queue as it arrives (or
    discarding output entirely if quiet=True -- used for the final
    machine.reset() call, whose own disconnect always prints a real Python
    traceback from mpremote's side even though the reset itself is exactly
    what we wanted; showing that raw traceback would look like a failure to
    someone unfamiliar with this board's quirks, so it's silenced rather
    than merely tolerated). Returns True on success, or if allow_fail=True,
    regardless of exit code -- also used for the watchdog-extend call on
    firmware that predates machine.WDT, where failing is fine, just means
    there's nothing to extend."""
    log_queue.put(("line", "$ " + " ".join(cmd)))
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL if quiet else subprocess.PIPE,
            stderr=subprocess.DEVNULL if quiet else subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except FileNotFoundError as e:
        log_queue.put(("line", "ERROR: %s" % e))
        return False
    if not quiet:
        for line in proc.stdout:
            log_queue.put(("line", line.rstrip("\n")))
    proc.wait()
    ok = proc.returncode == 0
    if not ok and not allow_fail:
        log_queue.put(("line", "FAILED (exit code %d)" % proc.returncode))
    return ok or allow_fail


def flash_firmware(port, log_queue):
    """Runs in a background thread. Returns True on success."""
    for _, path in FLASH_PARTS:
        if not os.path.exists(path):
            log_queue.put(("line", "ERROR: missing firmware file: %s" % path))
            log_queue.put(("line", "See firmware_bin/README.md for how to (re)populate this directory."))
            return False

    cmd = [
        sys.executable, "-m", "esptool",
        "--chip", "esp32s3", "-p", port, "-b", FLASH_BAUD,
        "--before", "default_reset", "--after", "hard_reset",
        "write_flash", "--flash_mode", "dio", "--flash_freq", "80m", "--flash_size", "4MB",
    ]
    for addr, path in FLASH_PARTS:
        cmd += [addr, path]
    return _run_streamed(cmd, log_queue)


def upload_code(port, log_queue):
    """Runs in a background thread. Returns True on success."""
    # Best-effort -- allow_fail because firmware flashed before the
    # watchdog existed at all would make this exec fail with an
    # AttributeError, which is fine, just means there's nothing to extend.
    _run_streamed(
        [sys.executable, "-m", "mpremote", "connect", port, "exec",
         "import machine; machine.WDT(timeout=%d)" % WATCHDOG_EXTEND_TIMEOUT_MS],
        log_queue, allow_fail=True,
    )

    for fname in UPLOAD_FILES:
        src = os.path.join(FIRMWARE_DIR, fname)
        if not os.path.exists(src):
            log_queue.put(("line", "SKIP (not found on disk): %s" % fname))
            continue
        ok = _run_streamed(
            [sys.executable, "-m", "mpremote", "connect", port, "fs", "cp", src, ":" + fname],
            log_queue,
        )
        if not ok:
            return False

    log_queue.put(("line", "Resetting device so it boots fresh with the real watchdog timeout..."))
    # The device's own USB CDC always drops mid-command for a real reset
    # (confirmed repeatedly during development), which surfaces as a real
    # Python traceback from mpremote's side -- expected, not a failure,
    # hence allow_fail=True and quiet=True (see _run_streamed's docstring).
    _run_streamed(
        [sys.executable, "-m", "mpremote", "connect", port, "exec", "import machine; machine.reset()"],
        log_queue, allow_fail=True, quiet=True,
    )
    log_queue.put(("line", "Done."))
    return True


class WifiSerialLink:
    """Owns the WiFi-setup serial connection and a background reader thread
    that pushes every line the device prints into a queue for the UI thread
    to consume. Separate from the flash/upload path (which shells out to
    esptool/mpremote per-invocation) -- this one holds the port open for as
    long as the WiFi panel is "connected", so the app must release it before
    any esptool/mpremote command needs the same port."""

    def __init__(self, line_queue):
        self.ser = None
        self.line_queue = line_queue
        self._stop = threading.Event()
        self._thread = None

    def connect(self, port):
        self.disconnect()
        self.ser = serial.Serial(port, WIFI_BAUD_RATE, timeout=0.2)
        self._stop.clear()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def disconnect(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
            self._thread = None
        if self.ser is not None:
            try:
                self._reset_device()
            except Exception:
                pass
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def _reset_device(self):
        """Pulses the board's EN/reset line so it reboots and re-runs
        main.py cleanly, instead of being left in whatever state this app's
        serial session put it in -- avoids needing a manual physical reset
        after using this app. Same DTR/RTS sequence esptool uses for a
        "hard reset" (not bootloader mode)."""
        self.ser.dtr = False
        time.sleep(0.1)
        self.ser.rts = True
        time.sleep(0.1)
        self.ser.rts = False
        time.sleep(0.1)
        self.ser.dtr = True

    @property
    def is_connected(self):
        return self.ser is not None and self.ser.is_open

    def send_command(self, obj):
        if not self.is_connected:
            raise RuntimeError("not connected")
        line = json.dumps(obj) + "\n"
        self.ser.write(line.encode("utf-8"))
        self.ser.flush()

    def _read_loop(self):
        while not self._stop.is_set():
            try:
                raw = self.ser.readline()
            except (serial.SerialException, OSError):
                self.line_queue.put(("closed", None))
                return
            if raw:
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                if text:
                    self.line_queue.put(("line", text))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ACC Flash App")
        self.geometry("680x900")
        self.minsize(600, 720)

        self.log_queue = queue.Queue()
        self._busy = False

        self.wifi_line_queue = queue.Queue()
        self.wifi_link = WifiSerialLink(self.wifi_line_queue)
        self._wifi_awaiting_cmd = None
        self._wifi_awaiting_quiet = False
        self._wifi_awaiting_token = 0
        self._wifi_timeout_after_id = None
        self._wifi_scan_results = {}  # display string -> raw ssid

        self._build_ui()
        self._refresh_ports()
        self.after(100, self._poll_queues)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------------- UI ---
    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        conn_frame = ttk.LabelFrame(self, text="Port")
        conn_frame.pack(fill="x", **pad)

        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(conn_frame, textvariable=self.port_var, state="readonly", width=30)
        self.port_combo.grid(row=0, column=0, padx=6, pady=6, sticky="we")
        ttk.Button(conn_frame, text="Refresh", command=self._refresh_ports).grid(row=0, column=1, padx=6, pady=6)
        conn_frame.columnconfigure(0, weight=1)

        flash_frame = ttk.LabelFrame(self, text="Step 1: Flash Firmware")
        flash_frame.pack(fill="x", **pad)
        ttk.Label(
            flash_frame,
            text="N16R2 (quad PSRAM) + BMI270 build. Needs a manual BOOT+RESET first -- the app will prompt you.",
            wraplength=600, justify="left",
        ).pack(anchor="w", padx=6, pady=(6, 0))
        self.flash_btn = ttk.Button(flash_frame, text="Flash Firmware", command=self._on_flash_clicked)
        self.flash_btn.pack(anchor="w", padx=6, pady=6)

        upload_frame = ttk.LabelFrame(self, text="Step 2: Upload Code")
        upload_frame.pack(fill="x", **pad)
        ttk.Label(
            upload_frame,
            text="Uploads: " + ", ".join(UPLOAD_FILES),
            wraplength=600, justify="left",
        ).pack(anchor="w", padx=6, pady=(6, 0))
        self.upload_btn = ttk.Button(upload_frame, text="Upload Code", command=self._on_upload_clicked)
        self.upload_btn.pack(anchor="w", padx=6, pady=6)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status_var, foreground="blue").pack(fill="x", padx=8)

        self._build_wifi_ui(pad)

        log_frame = ttk.LabelFrame(self, text="Log")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=14, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

    def _build_wifi_ui(self, pad):
        wifi_outer = ttk.LabelFrame(self, text="Step 3: WiFi Setup")
        wifi_outer.pack(fill="x", **pad)

        self.wifi_connect_btn = ttk.Button(wifi_outer, text="Connect", command=self._toggle_wifi_connect)
        self.wifi_connect_btn.grid(row=0, column=0, padx=6, pady=6, sticky="w")
        self.wifi_status_var = tk.StringVar(value="Not connected")
        ttk.Label(wifi_outer, textvariable=self.wifi_status_var).grid(
            row=0, column=1, columnspan=3, padx=6, pady=6, sticky="w")

        self.saved_ssid_var = tk.StringVar(value="Saved SSID: (connect and refresh to check)")
        ttk.Label(wifi_outer, textvariable=self.saved_ssid_var).grid(
            row=1, column=0, columnspan=2, padx=6, pady=(2, 2), sticky="w")

        saved_btn_row = ttk.Frame(wifi_outer)
        saved_btn_row.grid(row=1, column=2, columnspan=2, padx=6, pady=(2, 2), sticky="e")
        self.wifi_refresh_saved_btn = ttk.Button(saved_btn_row, text="Refresh", command=self._send_wifi_get)
        self.wifi_refresh_saved_btn.pack(side="left", padx=(0, 4))
        self.wifi_clear_saved_btn = ttk.Button(saved_btn_row, text="Clear Saved", command=self._send_wifi_clear)
        self.wifi_clear_saved_btn.pack(side="left")

        ttk.Label(wifi_outer, text="Networks:").grid(row=2, column=0, padx=6, pady=4, sticky="e")
        self.wifi_networks_var = tk.StringVar()
        self.wifi_networks_combo = ttk.Combobox(
            wifi_outer, textvariable=self.wifi_networks_var, state="readonly", width=28)
        self.wifi_networks_combo.grid(row=2, column=1, columnspan=2, padx=6, pady=4, sticky="we")
        self.wifi_networks_combo.bind("<<ComboboxSelected>>", self._on_wifi_network_selected)
        self.wifi_scan_btn = ttk.Button(wifi_outer, text="Scan", command=self._send_wifi_scan)
        self.wifi_scan_btn.grid(row=2, column=3, padx=6, pady=4)

        ttk.Label(wifi_outer, text="SSID:").grid(row=3, column=0, padx=6, pady=4, sticky="e")
        self.wifi_ssid_entry = ttk.Entry(wifi_outer, width=32)
        self.wifi_ssid_entry.grid(row=3, column=1, columnspan=3, padx=6, pady=4, sticky="we")

        ttk.Label(wifi_outer, text="Password:").grid(row=4, column=0, padx=6, pady=4, sticky="e")
        self.wifi_password_entry = ttk.Entry(wifi_outer, width=32, show="*")
        self.wifi_password_entry.grid(row=4, column=1, columnspan=2, padx=6, pady=4, sticky="we")

        self.wifi_show_pw_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            wifi_outer, text="Show", variable=self.wifi_show_pw_var, command=self._toggle_wifi_password_visibility
        ).grid(row=4, column=3, padx=6, pady=4)

        wifi_btn_row = ttk.Frame(wifi_outer)
        wifi_btn_row.grid(row=5, column=0, columnspan=4, pady=(8, 6))
        self.wifi_save_btn = ttk.Button(wifi_btn_row, text="Save to Device", command=self._send_wifi_set)
        self.wifi_save_btn.pack(side="left", padx=6)
        self.wifi_test_btn = ttk.Button(wifi_btn_row, text="Test Connection", command=self._send_wifi_test)
        self.wifi_test_btn.pack(side="left", padx=6)

        wifi_outer.columnconfigure(1, weight=1)

        self._set_wifi_controls_enabled(False)

    def _set_wifi_controls_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        combo_state = "readonly" if enabled else "disabled"
        for w in (self.wifi_refresh_saved_btn, self.wifi_clear_saved_btn, self.wifi_ssid_entry,
                  self.wifi_password_entry, self.wifi_save_btn, self.wifi_test_btn, self.wifi_scan_btn):
            w.configure(state=state)
        self.wifi_networks_combo.configure(state=combo_state)

    def _toggle_wifi_password_visibility(self):
        self.wifi_password_entry.configure(show="" if self.wifi_show_pw_var.get() else "*")

    def _on_wifi_network_selected(self, _event=None):
        ssid = self._wifi_scan_results.get(self.wifi_networks_var.get())
        if ssid is not None:
            self.wifi_ssid_entry.delete(0, "end")
            self.wifi_ssid_entry.insert(0, ssid)
            self.wifi_password_entry.focus_set()

    # ------------------------------------------------------------- ports ---
    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

    def _selected_port(self):
        port = self.port_var.get()
        if not port:
            messagebox.showwarning("No port selected", "Pick a serial port first (Refresh if it's not listed).")
            return None
        return port

    # ------------------------------------------------------------ actions --
    def _set_busy(self, busy, status=""):
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.flash_btn.configure(state=state)
        self.upload_btn.configure(state=state)
        self.wifi_connect_btn.configure(state=state)
        if status:
            self.status_var.set(status)

    def _release_wifi_link_for_subprocess(self):
        """Flash/Upload shell out to esptool/mpremote, which need exclusive
        access to the serial port. If the WiFi panel is holding it open,
        release it first -- otherwise the subprocess call fails with a
        "port busy" error instead of a clear one."""
        if self.wifi_link.is_connected:
            self._log("Disconnecting WiFi setup link so the port is free for this operation...")
            self.wifi_link.disconnect()
            self.wifi_connect_btn.configure(text="Connect")
            self.wifi_status_var.set("Not connected (disconnected automatically to free the port)")
            self._set_wifi_controls_enabled(False)

    def _on_flash_clicked(self):
        if self._busy:
            return
        port = self._selected_port()
        if not port:
            return
        if not messagebox.askokcancel("Manual BOOT+RESET required", BOOT_RESET_INSTRUCTIONS):
            return
        self._release_wifi_link_for_subprocess()
        self._log("--- Flashing firmware on %s ---" % port)
        self._set_busy(True, "Flashing...")
        threading.Thread(target=self._flash_thread, args=(port,), daemon=True).start()

    def _flash_thread(self, port):
        ok = flash_firmware(port, self.log_queue)
        self.log_queue.put(("flash_done", ok))

    def _on_upload_clicked(self):
        if self._busy:
            return
        port = self._selected_port()
        if not port:
            return
        self._release_wifi_link_for_subprocess()
        self._log("--- Uploading code to %s ---" % port)
        self._set_busy(True, "Uploading...")
        threading.Thread(target=self._upload_thread, args=(port,), daemon=True).start()

    def _upload_thread(self, port):
        ok = upload_code(port, self.log_queue)
        self.log_queue.put(("upload_done", ok))

    # -------------------------------------------------------- WiFi actions --
    def _toggle_wifi_connect(self):
        if self._busy:
            messagebox.showwarning(
                "Busy", "Wait for the current flash/upload to finish before connecting for WiFi setup.")
            return

        if self.wifi_link.is_connected:
            self.wifi_link.disconnect()
            self.wifi_connect_btn.configure(text="Connect")
            self.wifi_status_var.set("Not connected")
            self._set_wifi_controls_enabled(False)
            return

        port = self._selected_port()
        if not port:
            return
        try:
            self.wifi_link.connect(port)
        except Exception as e:
            messagebox.showerror("Connection failed", str(e))
            return

        self.wifi_connect_btn.configure(text="Disconnect")
        self.wifi_status_var.set("Connected to %s -- pinging device..." % port)
        self._set_wifi_controls_enabled(True)
        self._send_wifi_command("ping", {"cmd": "ping"})

    def _send_wifi_command(self, name, obj, quiet=False):
        """quiet=True skips overwriting status with a "waiting" message --
        used for background refreshes (e.g. re-fetching the saved SSID
        right after a save/clear) so they don't stomp on a success/failure
        message the user just triggered directly."""
        if not self.wifi_link.is_connected:
            messagebox.showwarning("Not connected", "Connect to the device first.")
            return
        try:
            self.wifi_link.send_command(obj)
        except Exception as e:
            messagebox.showerror("Send failed", str(e))
            return

        self._wifi_awaiting_token += 1
        token = self._wifi_awaiting_token
        self._wifi_awaiting_cmd = name
        self._wifi_awaiting_quiet = quiet
        if not quiet:
            self.wifi_status_var.set("%s: waiting for device..." % name)

        if self._wifi_timeout_after_id is not None:
            self.after_cancel(self._wifi_timeout_after_id)
        timeout_ms = WIFI_TIMEOUT_MS.get(name, 5000)
        self._wifi_timeout_after_id = self.after(timeout_ms, lambda: self._on_wifi_timeout(token))

    def _on_wifi_timeout(self, token):
        if token != self._wifi_awaiting_token or self._wifi_awaiting_cmd is None:
            return
        cmd = self._wifi_awaiting_cmd
        self.wifi_status_var.set("%s: no response (timed out)" % cmd)
        self._wifi_awaiting_cmd = None
        if cmd == "wifi_test":
            messagebox.showerror("Connection test failed", "No response from the device (timed out).")

    def _send_wifi_get(self, quiet=False):
        self._send_wifi_command("wifi_get", {"cmd": "wifi_get"}, quiet=quiet)

    def _send_wifi_set(self):
        ssid = self.wifi_ssid_entry.get().strip()
        password = self.wifi_password_entry.get()
        if not ssid:
            messagebox.showwarning("SSID required", "Enter a WiFi network name first.")
            return
        self._send_wifi_command("wifi_set", {"cmd": "wifi_set", "ssid": ssid, "password": password})

    def _send_wifi_clear(self):
        if not messagebox.askyesno(
            "Clear saved WiFi credentials?",
            "This deletes the WiFi network and password stored on the device.\n\n"
            "It won't affect a WiFi connection the device is currently using, but the "
            "next time it tries to upload it'll have no saved network unless "
            "WIFI_SSID/WIFI_PASSWORD are set in config.py as a fallback.",
        ):
            return
        self._send_wifi_command("wifi_clear", {"cmd": "wifi_clear"})

    def _send_wifi_test(self):
        self._send_wifi_command("wifi_test", {"cmd": "wifi_test"})

    def _send_wifi_scan(self):
        self._send_wifi_command("wifi_scan", {"cmd": "wifi_scan"})

    def _handle_wifi_link_closed(self):
        self.wifi_status_var.set("Device disconnected")
        self.wifi_connect_btn.configure(text="Connect")
        self._set_wifi_controls_enabled(False)
        self.wifi_link.disconnect()

    def _handle_wifi_line(self, text):
        self._log(text)

        if not text.startswith(WIFI_RESPONSE_PREFIX):
            return  # plain debug print from the device, not a protocol reply

        try:
            msg = json.loads(text[len(WIFI_RESPONSE_PREFIX):])
        except ValueError:
            return

        cmd = self._wifi_awaiting_cmd
        quiet = self._wifi_awaiting_quiet
        if self._wifi_timeout_after_id is not None:
            self.after_cancel(self._wifi_timeout_after_id)
            self._wifi_timeout_after_id = None
        self._wifi_awaiting_cmd = None
        self._wifi_awaiting_quiet = False

        if cmd == "ping":
            if msg.get("ok"):
                self.wifi_status_var.set("Connected -- device responded (%s v%s)" % (
                    msg.get("pong", "?"), msg.get("version", "?")))
                self._send_wifi_get()
            else:
                self.wifi_status_var.set("Connected, but device gave an unexpected ping reply")

        elif cmd == "wifi_get":
            ssid = msg.get("ssid")
            self.saved_ssid_var.set("Saved SSID: %s" % (ssid if ssid else "(none set)"))
            if not quiet:
                self.wifi_status_var.set("")

        elif cmd == "wifi_set":
            if msg.get("ok"):
                self.wifi_status_var.set("Saved to device.")
                self._send_wifi_get(quiet=True)
            else:
                self.wifi_status_var.set("Save failed: %s" % msg.get("error", "unknown error"))

        elif cmd == "wifi_clear":
            if msg.get("ok"):
                self.wifi_status_var.set(
                    "Cleared saved credentials." if msg.get("cleared") else "Nothing was saved to clear.")
                self._send_wifi_get(quiet=True)
            else:
                self.wifi_status_var.set("Clear failed: %s" % msg.get("error", "unknown error"))

        elif cmd == "wifi_test":
            if msg.get("ok"):
                ip = msg.get("ip", "?")
                self.wifi_status_var.set("Connected! Device IP: %s" % ip)
                messagebox.showinfo("Connection test succeeded", "The device connected to WiFi.\n\nIP address: %s" % ip)
            else:
                error = msg.get("error", "unknown error")
                self.wifi_status_var.set("Connection test failed: %s" % error)
                messagebox.showerror("Connection test failed", error)

        elif cmd == "wifi_scan":
            if msg.get("ok"):
                self._populate_wifi_networks(msg.get("networks", []))
            else:
                self.wifi_status_var.set("Scan failed: %s" % msg.get("error", "unknown error"))

    def _populate_wifi_networks(self, networks):
        self._wifi_scan_results = {}
        display_values = []
        for net in networks:
            ssid = net.get("ssid", "")
            rssi = net.get("rssi")
            lock = "secured" if net.get("secure") else "open"
            display = "%s  (%s dBm, %s)" % (ssid, rssi if rssi is not None else "?", lock)
            self._wifi_scan_results[display] = ssid
            display_values.append(display)

        self.wifi_networks_combo["values"] = display_values
        if display_values:
            self.wifi_status_var.set("Found %d network(s)." % len(display_values))
        else:
            self.wifi_status_var.set("No networks found.")

    def _on_close(self):
        self.wifi_link.disconnect()
        self.destroy()

    # -------------------------------------------------------------- queue --
    def _poll_queues(self):
        try:
            while True:
                kind, payload = self.log_queue.get_nowait()
                if kind == "line":
                    self._log(payload)
                elif kind == "flash_done":
                    self._set_busy(False, "Flash succeeded." if payload else "Flash failed -- see log.")
                    if payload:
                        messagebox.showinfo("Flash succeeded", POST_FLASH_INSTRUCTIONS)
                    else:
                        messagebox.showerror(
                            "Flash failed",
                            "See the log for details. If it says \"No serial data received\", "
                            "the BOOT+RESET sequence likely wasn't timed right -- try again.",
                        )
                elif kind == "upload_done":
                    self._set_busy(False, "Upload succeeded." if payload else "Upload failed -- see log.")
                    if payload:
                        messagebox.showinfo("Upload succeeded", "Code uploaded and the device has been reset.")
                    else:
                        messagebox.showerror("Upload failed", "See the log for details.")
        except queue.Empty:
            pass

        try:
            while True:
                kind, payload = self.wifi_line_queue.get_nowait()
                if kind == "closed":
                    self._handle_wifi_link_closed()
                elif kind == "line":
                    self._handle_wifi_line(payload)
        except queue.Empty:
            pass

        self.after(100, self._poll_queues)

    def _log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


if __name__ == "__main__":
    App().mainloop()
