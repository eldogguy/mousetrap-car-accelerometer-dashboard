#!/usr/bin/env python3
"""Desktop companion app for configuring WiFi credentials on the ESP32-S3
9DOF IMU logger (MicroPython firmware) over USB serial.

Speaks the JSON-line protocol implemented in ../serial_provisioning.py:
ping / wifi_get / wifi_set / wifi_test. Plug the board in, pick its serial
port, connect, and set the SSID/password -- no need to touch the device's
source files or reflash anything.

Usage:
    pip3 install -r requirements.txt
    python3 app.py
"""

import json
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import serial
import serial.tools.list_ports

BAUD_RATE = 115200
RESPONSE_PREFIX = "@IMU@"

TIMEOUT_MS = {
    "ping": 3000,
    "wifi_get": 3000,
    "wifi_set": 3000,
    "wifi_clear": 3000,
    "wifi_test": 20000,  # the device itself waits up to ~15s before giving up
    "wifi_scan": 10000,  # radio scan itself typically takes a few seconds
}


class SerialLink:
    """Owns the serial connection and a background reader thread that pushes
    every line the device prints into a queue for the UI thread to consume."""

    def __init__(self, line_queue):
        self.ser = None
        self.line_queue = line_queue
        self._stop = threading.Event()
        self._thread = None

    def connect(self, port):
        self.disconnect()
        self.ser = serial.Serial(port, BAUD_RATE, timeout=0.2)
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
        self.title("ESP32 9DOF IMU - WiFi Setup")
        self.geometry("560x560")
        self.minsize(480, 460)

        self.line_queue = queue.Queue()
        self.link = SerialLink(self.line_queue)

        self._awaiting_cmd = None
        self._awaiting_quiet = False
        self._awaiting_token = 0
        self._timeout_after_id = None

        self._build_ui()
        self._refresh_ports()
        self.after(100, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------------- UI ---
    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        conn_frame = ttk.LabelFrame(self, text="Connection")
        conn_frame.pack(fill="x", **pad)

        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(conn_frame, textvariable=self.port_var, state="readonly", width=30)
        self.port_combo.grid(row=0, column=0, padx=6, pady=6, sticky="we")

        ttk.Button(conn_frame, text="Refresh Ports", command=self._refresh_ports).grid(row=0, column=1, padx=6, pady=6)
        self.connect_btn = ttk.Button(conn_frame, text="Connect", command=self._toggle_connect)
        self.connect_btn.grid(row=0, column=2, padx=6, pady=6)

        self.status_var = tk.StringVar(value="Not connected")
        ttk.Label(conn_frame, textvariable=self.status_var).grid(row=1, column=0, columnspan=3, padx=6, sticky="w")

        conn_frame.columnconfigure(0, weight=1)

        wifi_frame = ttk.LabelFrame(self, text="WiFi Credentials")
        wifi_frame.pack(fill="x", **pad)

        self.saved_ssid_var = tk.StringVar(value="Saved SSID: (connect and refresh to check)")
        ttk.Label(wifi_frame, textvariable=self.saved_ssid_var).grid(
            row=0, column=0, columnspan=2, padx=6, pady=(6, 2), sticky="w")

        saved_btn_row = ttk.Frame(wifi_frame)
        saved_btn_row.grid(row=0, column=2, columnspan=2, padx=6, pady=(6, 2), sticky="e")
        self.refresh_saved_btn = ttk.Button(saved_btn_row, text="Refresh", command=self._send_wifi_get)
        self.refresh_saved_btn.pack(side="left", padx=(0, 4))
        self.clear_saved_btn = ttk.Button(saved_btn_row, text="Clear Saved", command=self._send_wifi_clear)
        self.clear_saved_btn.pack(side="left")

        ttk.Label(wifi_frame, text="Networks:").grid(row=1, column=0, padx=6, pady=4, sticky="e")
        self.networks_var = tk.StringVar()
        self.networks_combo = ttk.Combobox(wifi_frame, textvariable=self.networks_var, state="readonly", width=28)
        self.networks_combo.grid(row=1, column=1, columnspan=2, padx=6, pady=4, sticky="we")
        self.networks_combo.bind("<<ComboboxSelected>>", self._on_network_selected)
        self._scan_results = {}  # display string -> raw ssid
        self.scan_btn = ttk.Button(wifi_frame, text="Scan", command=self._send_wifi_scan)
        self.scan_btn.grid(row=1, column=3, padx=6, pady=4)

        ttk.Label(wifi_frame, text="SSID:").grid(row=2, column=0, padx=6, pady=4, sticky="e")
        self.ssid_entry = ttk.Entry(wifi_frame, width=32)
        self.ssid_entry.grid(row=2, column=1, columnspan=3, padx=6, pady=4, sticky="we")

        ttk.Label(wifi_frame, text="Password:").grid(row=3, column=0, padx=6, pady=4, sticky="e")
        self.password_entry = ttk.Entry(wifi_frame, width=32, show="*")
        self.password_entry.grid(row=3, column=1, columnspan=2, padx=6, pady=4, sticky="we")

        self.show_pw_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            wifi_frame, text="Show", variable=self.show_pw_var, command=self._toggle_password_visibility
        ).grid(row=3, column=3, padx=6, pady=4)

        btn_row = ttk.Frame(wifi_frame)
        btn_row.grid(row=4, column=0, columnspan=4, pady=(8, 6))
        self.save_btn = ttk.Button(btn_row, text="Save to Device", command=self._send_wifi_set)
        self.save_btn.pack(side="left", padx=6)
        self.test_btn = ttk.Button(btn_row, text="Test Connection", command=self._send_wifi_test)
        self.test_btn.pack(side="left", padx=6)

        wifi_frame.columnconfigure(1, weight=1)

        self.action_status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.action_status_var, foreground="blue").pack(fill="x", padx=8)

        log_frame = ttk.LabelFrame(self, text="Device log (raw serial output)")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=12, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

        self._set_wifi_controls_enabled(False)

    def _set_wifi_controls_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        combo_state = "readonly" if enabled else "disabled"
        for w in (self.refresh_saved_btn, self.clear_saved_btn, self.ssid_entry, self.password_entry,
                  self.save_btn, self.test_btn, self.scan_btn):
            w.configure(state=state)
        self.networks_combo.configure(state=combo_state)

    def _toggle_password_visibility(self):
        self.password_entry.configure(show="" if self.show_pw_var.get() else "*")

    def _on_network_selected(self, _event=None):
        ssid = self._scan_results.get(self.networks_var.get())
        if ssid is not None:
            self.ssid_entry.delete(0, "end")
            self.ssid_entry.insert(0, ssid)
            self.password_entry.focus_set()

    # ------------------------------------------------------------- ports ---
    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

    # --------------------------------------------------------- connection --
    def _toggle_connect(self):
        if self.link.is_connected:
            self.link.disconnect()
            self.connect_btn.configure(text="Connect")
            self.status_var.set("Not connected")
            self._set_wifi_controls_enabled(False)
            return

        port = self.port_var.get()
        if not port:
            messagebox.showwarning("No port selected", "Pick a serial port first.")
            return
        try:
            self.link.connect(port)
        except Exception as e:
            messagebox.showerror("Connection failed", str(e))
            return

        self.connect_btn.configure(text="Disconnect")
        self.status_var.set("Connected to %s -- pinging device..." % port)
        self._set_wifi_controls_enabled(True)
        self._send_command("ping", {"cmd": "ping"})

    def _on_close(self):
        self.link.disconnect()
        self.destroy()

    # ---------------------------------------------------------- commands ---
    def _send_command(self, name, obj, quiet=False):
        """quiet=True skips overwriting action_status_var with a "waiting"
        message -- used for background refreshes (e.g. re-fetching the
        saved SSID right after a save/clear) so they don't stomp on a
        success/failure message the user just triggered directly."""
        if not self.link.is_connected:
            messagebox.showwarning("Not connected", "Connect to the device first.")
            return
        try:
            self.link.send_command(obj)
        except Exception as e:
            messagebox.showerror("Send failed", str(e))
            return

        self._awaiting_token += 1
        token = self._awaiting_token
        self._awaiting_cmd = name
        self._awaiting_quiet = quiet
        if not quiet:
            self.action_status_var.set("%s: waiting for device..." % name)

        if self._timeout_after_id is not None:
            self.after_cancel(self._timeout_after_id)
        timeout_ms = TIMEOUT_MS.get(name, 5000)
        self._timeout_after_id = self.after(timeout_ms, lambda: self._on_timeout(token))

    def _on_timeout(self, token):
        if token != self._awaiting_token or self._awaiting_cmd is None:
            return
        cmd = self._awaiting_cmd
        self.action_status_var.set("%s: no response (timed out)" % cmd)
        self._awaiting_cmd = None
        if cmd == "wifi_test":
            messagebox.showerror("Connection test failed", "No response from the device (timed out).")

    def _send_wifi_get(self, quiet=False):
        self._send_command("wifi_get", {"cmd": "wifi_get"}, quiet=quiet)

    def _send_wifi_set(self):
        ssid = self.ssid_entry.get().strip()
        password = self.password_entry.get()
        if not ssid:
            messagebox.showwarning("SSID required", "Enter a WiFi network name first.")
            return
        self._send_command("wifi_set", {"cmd": "wifi_set", "ssid": ssid, "password": password})

    def _send_wifi_clear(self):
        if not messagebox.askyesno(
            "Clear saved WiFi credentials?",
            "This deletes the WiFi network and password stored on the device.\n\n"
            "It won't affect a WiFi connection the device is currently using, but the "
            "next time it tries to upload it'll have no saved network unless "
            "WIFI_SSID/WIFI_PASSWORD are set in config.py as a fallback.",
        ):
            return
        self._send_command("wifi_clear", {"cmd": "wifi_clear"})

    def _send_wifi_test(self):
        self._send_command("wifi_test", {"cmd": "wifi_test"})

    def _send_wifi_scan(self):
        self._send_command("wifi_scan", {"cmd": "wifi_scan"})

    # -------------------------------------------------------------- queue --
    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.line_queue.get_nowait()
                if kind == "closed":
                    self._handle_link_closed()
                elif kind == "line":
                    self._handle_line(payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _handle_link_closed(self):
        self.status_var.set("Device disconnected")
        self.connect_btn.configure(text="Connect")
        self._set_wifi_controls_enabled(False)
        self.link.disconnect()

    def _handle_line(self, text):
        self._log(text)

        if not text.startswith(RESPONSE_PREFIX):
            return  # plain debug print from the device, not a protocol reply

        try:
            msg = json.loads(text[len(RESPONSE_PREFIX):])
        except ValueError:
            return

        cmd = self._awaiting_cmd
        quiet = self._awaiting_quiet
        if self._timeout_after_id is not None:
            self.after_cancel(self._timeout_after_id)
            self._timeout_after_id = None
        self._awaiting_cmd = None
        self._awaiting_quiet = False

        if cmd == "ping":
            if msg.get("ok"):
                self.status_var.set("Connected -- device responded (%s v%s)" % (
                    msg.get("pong", "?"), msg.get("version", "?")))
                self._send_wifi_get()
            else:
                self.status_var.set("Connected, but device gave an unexpected ping reply")

        elif cmd == "wifi_get":
            ssid = msg.get("ssid")
            self.saved_ssid_var.set("Saved SSID: %s" % (ssid if ssid else "(none set)"))
            if not quiet:
                self.action_status_var.set("")

        elif cmd == "wifi_set":
            if msg.get("ok"):
                self.action_status_var.set("Saved to device.")
                self._send_wifi_get(quiet=True)
            else:
                self.action_status_var.set("Save failed: %s" % msg.get("error", "unknown error"))

        elif cmd == "wifi_clear":
            if msg.get("ok"):
                self.action_status_var.set(
                    "Cleared saved credentials." if msg.get("cleared") else "Nothing was saved to clear.")
                self._send_wifi_get(quiet=True)
            else:
                self.action_status_var.set("Clear failed: %s" % msg.get("error", "unknown error"))

        elif cmd == "wifi_test":
            if msg.get("ok"):
                ip = msg.get("ip", "?")
                self.action_status_var.set("Connected! Device IP: %s" % ip)
                messagebox.showinfo("Connection test succeeded", "The device connected to WiFi.\n\nIP address: %s" % ip)
            else:
                error = msg.get("error", "unknown error")
                self.action_status_var.set("Connection test failed: %s" % error)
                messagebox.showerror("Connection test failed", error)

        elif cmd == "wifi_scan":
            if msg.get("ok"):
                self._populate_networks(msg.get("networks", []))
            else:
                self.action_status_var.set("Scan failed: %s" % msg.get("error", "unknown error"))

    def _populate_networks(self, networks):
        self._scan_results = {}
        display_values = []
        for net in networks:
            ssid = net.get("ssid", "")
            rssi = net.get("rssi")
            lock = "secured" if net.get("secure") else "open"
            display = "%s  (%s dBm, %s)" % (ssid, rssi if rssi is not None else "?", lock)
            self._scan_results[display] = ssid
            display_values.append(display)

        self.networks_combo["values"] = display_values
        if display_values:
            self.action_status_var.set("Found %d network(s)." % len(display_values))
        else:
            self.action_status_var.set("No networks found.")

    def _log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


if __name__ == "__main__":
    App().mainloop()
