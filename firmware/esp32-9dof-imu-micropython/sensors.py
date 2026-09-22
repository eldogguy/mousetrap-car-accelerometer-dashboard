"""Register-level I2C drivers for the L3GD20 gyroscope and the LSM303DLHC
accelerometer/magnetometer, ported from the C++ drivers in
../esp32-9dof-imu/src/. See that project's README for the underlying
register-map notes (WHO_AM_I values, the magnetometer's X/Z/Y read order
quirk, etc).
"""

import struct
import utime

_AUTO_INCREMENT = 0x80  # OR into a register address for multi-byte reads


class L3GD20:
    SCALE_250DPS = 0x00
    SCALE_500DPS = 0x01
    SCALE_2000DPS = 0x02

    _REG_WHO_AM_I = 0x0F
    _REG_CTRL_REG1 = 0x20
    _REG_CTRL_REG4 = 0x23
    _REG_OUT_X_L = 0x28
    _WHO_AM_I_VALUE = 0xD4  # 0xD7 on the L3GD20H variant

    _SENSITIVITY = {
        SCALE_250DPS: 0.00875,
        SCALE_500DPS: 0.0175,
        SCALE_2000DPS: 0.070,
    }

    def __init__(self, i2c, address=0x6B, scale=SCALE_500DPS):
        self.i2c = i2c
        self.addr = address
        self.sensitivity = self._SENSITIVITY[scale]
        self.scale = scale

    def begin(self):
        try:
            who = self.i2c.readfrom_mem(self.addr, self._REG_WHO_AM_I, 1)[0]
        except OSError:
            return False
        if who != self._WHO_AM_I_VALUE:
            return False

        # ODR=100Hz, cutoff=12.5Hz, normal power mode, X/Y/Z enabled
        self.i2c.writeto_mem(self.addr, self._REG_CTRL_REG1, bytes([0x0F]))
        # Full-scale select
        self.i2c.writeto_mem(self.addr, self._REG_CTRL_REG4, bytes([(self.scale & 0x03) << 4]))
        utime.sleep_ms(100)
        return True

    def read_dps(self):
        """Returns (x, y, z) in degrees/second, or None on I2C failure."""
        try:
            buf = self.i2c.readfrom_mem(self.addr, self._REG_OUT_X_L | _AUTO_INCREMENT, 6)
        except OSError:
            return None
        rx, ry, rz = struct.unpack("<hhh", buf)
        s = self.sensitivity
        return (rx * s, ry * s, rz * s)


class LSM303DLHCAccel:
    """Accelerometer half of the LSM303DLHC (fixed I2C address 0x19).
    Runs in high-resolution mode, +/-4g range (2 mg/LSB)."""

    _REG_CTRL_REG1_A = 0x20
    _REG_CTRL_REG4_A = 0x23
    _REG_OUT_X_L_A = 0x28
    _SENSITIVITY_G_PER_LSB = 0.002

    def __init__(self, i2c, address=0x19):
        self.i2c = i2c
        self.addr = address

    def begin(self):
        try:
            # 100Hz ODR, normal power, X/Y/Z enabled -> 0b01010111
            self.i2c.writeto_mem(self.addr, self._REG_CTRL_REG1_A, bytes([0x57]))
            # No WHO_AM_I register on this sensor -- confirm presence by
            # reading back what was just written.
            readback = self.i2c.readfrom_mem(self.addr, self._REG_CTRL_REG1_A, 1)[0]
            if readback != 0x57:
                return False
            # High-resolution mode, +/-4g range -> 0b00011000
            self.i2c.writeto_mem(self.addr, self._REG_CTRL_REG4_A, bytes([0x18]))
        except OSError:
            return False
        utime.sleep_ms(10)
        return True

    def read_g(self):
        """Returns (x, y, z) in g, or None on I2C failure."""
        try:
            buf = self.i2c.readfrom_mem(self.addr, self._REG_OUT_X_L_A | _AUTO_INCREMENT, 6)
        except OSError:
            return None
        rx, ry, rz = struct.unpack("<hhh", buf)
        # High-res output is left-justified in the 16-bit register; shift
        # right by 4 to get the 12-bit signed value the sensitivity expects.
        s = self._SENSITIVITY_G_PER_LSB
        return ((rx >> 4) * s, (ry >> 4) * s, (rz >> 4) * s)


class LSM303DLHCMag:
    """Magnetometer half of the LSM303DLHC (fixed I2C address 0x1E). Same
    underlying sensor core as the HMC5883L, so it shares its X/Z/Y output
    register order and identification registers.

    No hard-iron/soft-iron calibration is performed here -- readings are
    raw sensor output converted to microtesla.
    """

    _REG_CRA_REG_M = 0x00
    _REG_CRB_REG_M = 0x01
    _REG_MR_REG_M = 0x02
    _REG_OUT_X_H_M = 0x03
    _REG_IRA_REG_M = 0x0A

    _LSB_PER_GAUSS_XY = 1100.0
    _LSB_PER_GAUSS_Z = 980.0
    _UT_PER_GAUSS = 100.0

    def __init__(self, i2c, address=0x1E):
        self.i2c = i2c
        self.addr = address

    def begin(self):
        try:
            ida = self.i2c.readfrom_mem(self.addr, self._REG_IRA_REG_M, 1)[0]
            if ida != ord("H"):
                return False
            self.i2c.writeto_mem(self.addr, self._REG_CRA_REG_M, bytes([0x10]))  # 15Hz, no temp
            self.i2c.writeto_mem(self.addr, self._REG_CRB_REG_M, bytes([0x20]))  # +/-1.3 Ga
            self.i2c.writeto_mem(self.addr, self._REG_MR_REG_M, bytes([0x00]))   # continuous mode
        except OSError:
            return False
        utime.sleep_ms(10)
        return True

    def read_ut(self):
        """Returns (x, y, z) in microtesla, or None on I2C failure."""
        try:
            buf = self.i2c.readfrom_mem(self.addr, self._REG_OUT_X_H_M, 6)
        except OSError:
            return None
        # Register order is X, Z, Y (not X, Y, Z) -- inherited from the
        # HMC5883L core, and read big-endian (MSB first).
        rx, rz, ry = struct.unpack(">hhh", buf)
        x = (rx / self._LSB_PER_GAUSS_XY) * self._UT_PER_GAUSS
        y = (ry / self._LSB_PER_GAUSS_XY) * self._UT_PER_GAUSS
        z = (rz / self._LSB_PER_GAUSS_Z) * self._UT_PER_GAUSS
        return (x, y, z)
