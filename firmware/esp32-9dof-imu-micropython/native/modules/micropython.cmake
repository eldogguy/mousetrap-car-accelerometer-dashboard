# Top-level USER_C_MODULES entry point -- lists the individual native
# modules to include. Paths are absolute; ${CMAKE_CURRENT_LIST_DIR} prefixes
# subdirectories. See ../README.md for the build command that references
# this file.

include(${CMAKE_CURRENT_LIST_DIR}/imu_sampler/micropython.cmake)
