"""Minimal I2C driver for the SSD1306 128x64 OLED, built on MicroPython's
framebuf module. Init/contrast/addressing-mode command sequence follows
the SSD1306 datasheet -- same category of driver as the register-level
sensor drivers in sensors.py, just for a display instead of a sensor.
"""

import framebuf

_SET_CONTRAST = 0x81
_SET_ENTIRE_ON = 0xA4
_SET_NORM_INV = 0xA6
_SET_DISP = 0xAE
_SET_MEM_ADDR = 0x20
_SET_COL_ADDR = 0x21
_SET_PAGE_ADDR = 0x22
_SET_DISP_START_LINE = 0x40
_SET_SEG_REMAP = 0xA0
_SET_MUX_RATIO = 0xA8
_SET_COM_OUT_DIR = 0xC0
_SET_DISP_OFFSET = 0xD3
_SET_COM_PIN_CFG = 0xDA
_SET_DISP_CLK_DIV = 0xD5
_SET_PRECHARGE = 0xD9
_SET_VCOM_DESEL = 0xDB
_SET_CHARGE_PUMP = 0x8D


class SSD1306_I2C:
    def __init__(self, width, height, i2c, addr=0x3C):
        self.width = width
        self.height = height
        self.i2c = i2c
        self.addr = addr
        self.pages = height // 8
        self.buffer = bytearray(self.pages * width)
        self.framebuf = framebuf.FrameBuffer(self.buffer, width, height, framebuf.MONO_VLSB)
        self.ok = False  # show() checks this; must exist before _init_display() can set it for real
        self.ok = self._init_display()
        if self.ok:
            self.fill(0)
            self.show()

    def _write_cmd(self, cmd):
        self.i2c.writeto(self.addr, bytearray([0x80, cmd]))

    def _write_data(self, buf):
        self.i2c.writeto(self.addr, b"\x40" + buf)

    def _init_display(self):
        try:
            for cmd in (
                _SET_DISP | 0x00,
                _SET_MEM_ADDR, 0x00,
                _SET_DISP_START_LINE | 0x00,
                _SET_SEG_REMAP | 0x01,
                _SET_MUX_RATIO, self.height - 1,
                _SET_COM_OUT_DIR | 0x08,
                _SET_DISP_OFFSET, 0x00,
                _SET_COM_PIN_CFG, 0x02 if self.height == 32 else 0x12,
                _SET_DISP_CLK_DIV, 0x80,
                _SET_PRECHARGE, 0xF1,
                _SET_VCOM_DESEL, 0x30,
                _SET_CONTRAST, 0xFF,
                _SET_ENTIRE_ON,
                _SET_NORM_INV,
                _SET_CHARGE_PUMP, 0x14,
                _SET_DISP | 0x01,
            ):
                self._write_cmd(cmd)
        except OSError:
            return False
        return True

    def fill(self, col):
        self.framebuf.fill(col)

    def text(self, string, x, y, col=1):
        self.framebuf.text(string, x, y, col)

    def text2x(self, string, x, y, col=1):
        """Same 8x8 built-in font, blitted at 2x scale -- for content that
        needs to be legible from across a table (e.g. the device key)."""
        w = 8 * len(string)
        tmp = framebuf.FrameBuffer(bytearray(w), w, 8, framebuf.MONO_VLSB)
        tmp.text(string, 0, 0, 1)
        for ty in range(8):
            for tx in range(w):
                if tmp.pixel(tx, ty):
                    self.framebuf.fill_rect(x + tx * 2, y + ty * 2, 2, 2, col)

    def show(self):
        if not self.ok:
            return
        self._write_cmd(_SET_COL_ADDR)
        self._write_cmd(0)
        self._write_cmd(self.width - 1)
        self._write_cmd(_SET_PAGE_ADDR)
        self._write_cmd(0)
        self._write_cmd(self.pages - 1)
        self._write_data(self.buffer)
