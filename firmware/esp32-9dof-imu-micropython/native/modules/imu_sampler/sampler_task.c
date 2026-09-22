#include "sampler_task.h"
#include "imu_backend.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/idf_additions.h" // xTaskCreatePinnedToCore -- ESP-IDF-specific, not in base FreeRTOS task.h on this IDF version
#include "freertos/queue.h"
#include "driver/i2c_master.h"
#include "esp_timer.h"
#include "esp_log.h"

#define SAMPLE_RATE_HZ 50
#define SAMPLE_PERIOD_MS (1000 / SAMPLE_RATE_HZ)
// Priority 5 is comfortably above both MP_TASK_PRIORITY and the _thread
// poll worker's priority (both ESP_TASK_PRIO_MIN + 1 == 1, confirmed by
// reading ports/esp32/main.c and mpthreadport.c) -- high enough that this
// task preempts them deterministically, low enough to stay well clear of
// WiFi/BT's own internal task priorities.
#define SAMPLER_TASK_PRIORITY 5
#define SAMPLER_TASK_STACK_WORDS 3072
#define SAMPLER_TASK_CORE 1 // MP_TASK_COREID -- see sampler_task.h
#define QUEUE_LEN 250       // 5s of headroom at 50Hz before a Python-side stall starts dropping samples

static const char *TAG = "imu_sampler";

static QueueHandle_t s_queue = NULL;
static TaskHandle_t s_task_handle = NULL;
static i2c_master_bus_handle_t s_bus = NULL;
static volatile uint32_t s_produced = 0;
static volatile uint32_t s_dropped = 0;
static volatile uint32_t s_queue_high_water = 0;
static int64_t s_start_us = 0;

static void imu_sampler_task(void *arg) {
    TickType_t last_wake = xTaskGetTickCount();
    const TickType_t period_ticks = pdMS_TO_TICKS(SAMPLE_PERIOD_MS);
    while (1) {
        vTaskDelayUntil(&last_wake, period_ticks);

        imu_raw_sample_t sample;
        if (!imu_backend_read(&sample)) {
            // Transient I2C failure -- skip this tick, try again next
            // period. Matches sensors.py's existing "return None on
            // OSError, caller just doesn't append a sample" behavior.
            continue;
        }
        sample.t_ms = (uint32_t)((esp_timer_get_time() - s_start_us) / 1000);

        s_produced++;
        if (xQueueSend(s_queue, &sample, 0) != pdTRUE) {
            s_dropped++;
        }
        UBaseType_t waiting = uxQueueMessagesWaiting(s_queue);
        if (waiting > s_queue_high_water) {
            s_queue_high_water = waiting;
        }
    }
}

bool imu_sampler_start(int sda_pin, int scl_pin, uint32_t freq_hz) {
    if (s_task_handle != NULL) {
        return true;
    }

    i2c_master_bus_config_t bus_config = {
        .i2c_port = I2C_NUM_1,
        .sda_io_num = sda_pin,
        .scl_io_num = scl_pin,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    if (i2c_new_master_bus(&bus_config, &s_bus) != ESP_OK) {
        ESP_LOGE(TAG, "failed to create I2C bus (sda=%d scl=%d)", sda_pin, scl_pin);
        s_bus = NULL;
        return false;
    }

    if (!imu_backend_init(s_bus)) {
        ESP_LOGE(TAG, "sensor backend init failed");
        i2c_del_master_bus(s_bus);
        s_bus = NULL;
        return false;
    }

    s_queue = xQueueCreate(QUEUE_LEN, sizeof(imu_raw_sample_t));
    if (s_queue == NULL) {
        ESP_LOGE(TAG, "failed to allocate sample queue");
        i2c_del_master_bus(s_bus);
        s_bus = NULL;
        return false;
    }

    s_start_us = esp_timer_get_time();
    s_produced = 0;
    s_dropped = 0;
    s_queue_high_water = 0;

    BaseType_t ok = xTaskCreatePinnedToCore(
        imu_sampler_task, "imu_sampler", SAMPLER_TASK_STACK_WORDS, NULL,
        SAMPLER_TASK_PRIORITY, &s_task_handle, SAMPLER_TASK_CORE);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "failed to create sampler task");
        s_task_handle = NULL;
        vQueueDelete(s_queue);
        s_queue = NULL;
        i2c_del_master_bus(s_bus);
        s_bus = NULL;
        return false;
    }

    ESP_LOGI(TAG, "started: sda=%d scl=%d freq=%lu core=%d prio=%d",
        sda_pin, scl_pin, (unsigned long)freq_hz, SAMPLER_TASK_CORE, SAMPLER_TASK_PRIORITY);
    return true;
}

int imu_sampler_drain(uint32_t *t_ms, float *gx, float *gy, float *gz,
    float *ax, float *ay, float *az, int out_start, int max_count) {
    if (s_queue == NULL) {
        return 0;
    }
    int n = 0;
    imu_raw_sample_t sample;
    while (n < max_count && xQueueReceive(s_queue, &sample, 0) == pdTRUE) {
        int idx = out_start + n;
        t_ms[idx] = sample.t_ms;
        gx[idx] = sample.gx;
        gy[idx] = sample.gy;
        gz[idx] = sample.gz;
        ax[idx] = sample.ax;
        ay[idx] = sample.ay;
        az[idx] = sample.az;
        n++;
    }
    return n;
}

void imu_sampler_get_stats(imu_sampler_stats_t *out) {
    out->produced = s_produced;
    out->dropped = s_dropped;
    out->queue_high_water = s_queue_high_water;
}

i2c_master_bus_handle_t imu_sampler_get_bus(void) {
    return s_bus;
}

uint32_t imu_sampler_now_ms(void) {
    return (uint32_t)((esp_timer_get_time() - s_start_us) / 1000);
}
