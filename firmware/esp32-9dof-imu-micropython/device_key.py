"""Stable per-chip 5-hex-digit identifier, shown on the OLED and used to
tag WiFi uploads so the web dashboard knows which device a run came from.

Derived from machine.unique_id() (the chip's factory-programmed ID), so it
stays the same across reboots/reflashes without needing to persist
anything to flash.
"""

import machine

try:
    import hashlib

    _HAVE_HASHLIB = True
except ImportError:
    _HAVE_HASHLIB = False


def compute_device_key():
    uid = machine.unique_id()
    if _HAVE_HASHLIB:
        digest = hashlib.sha256(uid).digest()
        val = int.from_bytes(digest[:3], "big")
    else:
        # Fallback if this build lacks hashlib: a simple deterministic
        # checksum over the unique ID bytes, same 20-bit output range.
        val = 0
        for b in uid:
            val = ((val << 5) ^ (val >> 3) ^ b) & 0xFFFFFF
    return "%05X" % (val & 0xFFFFF)
