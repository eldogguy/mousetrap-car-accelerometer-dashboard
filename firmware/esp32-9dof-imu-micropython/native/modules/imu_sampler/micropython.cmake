# Phase 1/2: real FreeRTOS sampler task + queue + swappable backend
# (L3GD20/LSM303DLHC or BMI270). Both backend_*.c files (and bmi270_platform.c)
# are always compiled; each guards its own content with #ifdef IMU_BACKEND_xxx
# (set via CFLAGS_EXTRA, see native/README.md) so exactly one backend produces
# real content -- swapping backends is a one-flag change, nothing else in
# this file or the task/queue/API changes. Bosch's vendored bmi2.c/bmi270.c
# (vendor/bosch_bmi270/) are always compiled too, even in an L3GD20 build --
# they're just unused object code then, not incorrect, and 16MB of flash has
# room to spare.

add_library(usermod_imu_native INTERFACE)

target_sources(usermod_imu_native INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/modimu_native.c
    ${CMAKE_CURRENT_LIST_DIR}/sampler_task.c
    ${CMAKE_CURRENT_LIST_DIR}/backend_l3gd20_lsm303.c
    ${CMAKE_CURRENT_LIST_DIR}/backend_bmi270.c
    ${CMAKE_CURRENT_LIST_DIR}/bmi270_platform.c
    ${CMAKE_CURRENT_LIST_DIR}/oled_ssd1306.c
    ${CMAKE_CURRENT_LIST_DIR}/audio_sdm.c
    ${CMAKE_CURRENT_LIST_DIR}/vendor/bosch_bmi270/bmi2.c
    ${CMAKE_CURRENT_LIST_DIR}/vendor/bosch_bmi270/bmi270.c
)

target_include_directories(usermod_imu_native INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
    ${CMAKE_CURRENT_LIST_DIR}/vendor/bosch_bmi270
)

target_link_libraries(usermod INTERFACE usermod_imu_native)
