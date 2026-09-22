# Bundled firmware binaries

`bootloader.bin`, `partition-table.bin`, and `micropython.bin` here are a
**pre-built** copy of the N16R2 (quad PSRAM) + BMI270 backend firmware --
the actual custom board's build, not the original N16R8/octal-PSRAM dev
board's. See `../../native/README.md` for the full build/architecture
story.

These are NOT rebuilt automatically by `app.py` -- it just flashes whatever
is sitting in this directory. If you make a native C change (anything
under `../../native/`) and rebuild, copy the fresh output back in here:

```bash
source ~/esp/esp-idf/export.sh
cd ~/esp/micropython/ports/esp32
make BOARD=ESP32_GENERIC_S3 \
     USER_C_MODULES=<path-to-repo>/firmware/esp32-9dof-imu-micropython/native/modules/micropython.cmake \
     CFLAGS_EXTRA=-DIMU_BACKEND_BMI270

cp build-ESP32_GENERIC_S3/bootloader/bootloader.bin \
   build-ESP32_GENERIC_S3/partition_table/partition-table.bin \
   build-ESP32_GENERIC_S3/micropython.bin \
   <path-to-repo>/firmware/esp32-9dof-imu-micropython/acc_flash_app/firmware_bin/
```

Plain Python file changes (`main.py`, `config.py`, etc.) don't need any of
this -- those are uploaded directly from the parent directory by the
"Upload Code" button, not baked into these binaries at all.
