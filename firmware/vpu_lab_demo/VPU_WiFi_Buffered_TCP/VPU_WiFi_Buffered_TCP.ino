#include <Arduino.h>
#include <WiFi.h>
#include <math.h>
#include <algorithm>
#include "esp_timer.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#define VPU_CLK D6
#define VPU_DAT D7

// -------- WIFI --------
const char* WIFI_SSID = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// -------- MAC --------
IPAddress MAC_IP(10, 0, 0, 4);
const int TCP_PORT = 5005;

WiFiClient client;

// -------- VPU --------
const int N = 5000;
const int CAL_SAMPLES = 600;

const float NOISE_MARGIN = 1.15;
const float MIN_CHANGE_THRESHOLD = 0.030;
const float DISPLAY_RANGE_MULTIPLIER = 18.0;

// 100 samples / second
const uint64_t SAMPLE_INTERVAL_US = 10000;

// Send 50 samples per TCP batch
const int BATCH_SIZE = 50;

// Buffer can hold about 20 seconds of samples
const int QUEUE_LENGTH = 2000;

struct Sample {
  uint64_t timestampUs;
  float vibration;
};

QueueHandle_t sampleQueue;

float previousActivity = 0.0;
float changeThreshold = 0.05;

volatile uint32_t droppedSamples = 0;

// --------------------------------------------------
// VPU
// --------------------------------------------------

float readActivity() {
  int highs = 0;

  for (int i = 0; i < N; i++) {
    if (digitalRead(VPU_DAT)) {
      highs++;
    }
  }

  return 100.0f * highs / N;
}

float scaleResponse(float excess) {
  if (excess <= 0) return 0;

  float relative = excess / changeThreshold;

  float score =
      100.0f * log1pf(relative) /
      log1pf(DISPLAY_RANGE_MULTIPLIER);

  if (score > 100) score = 100;
  if (score < 0) score = 0;

  return score;
}

// --------------------------------------------------
// TCP connection
// --------------------------------------------------

bool connectToMac() {
  if (client.connected()) {
    return true;
  }

  client.stop();

  Serial.println("Connecting to Mac...");

  if (client.connect(MAC_IP, TCP_PORT)) {
    client.setNoDelay(true);

    Serial.println("TCP connected");
    return true;
  }

  Serial.println("TCP connection failed");
  return false;
}

// --------------------------------------------------
// SENSOR TASK
//
// This task ONLY measures the VPU.
// It never waits for Wi-Fi.
// --------------------------------------------------

void samplingTask(void* parameter) {

  uint64_t nextSampleTime =
      esp_timer_get_time();

  while (true) {

    uint64_t now =
        esp_timer_get_time();

    // Wait until next sample time
    if (now < nextSampleTime) {
      uint64_t waitUs =
          nextSampleTime - now;

      if (waitUs > 2000) {
        vTaskDelay(
          pdMS_TO_TICKS(
            (waitUs - 1000) / 1000
          )
        );
      } else {
        delayMicroseconds(waitUs);
      }

      continue;
    }

    // If something delayed this task,
    // skip missed time slots instead of
    // taking fake back-to-back samples.
    if (now - nextSampleTime >=
        SAMPLE_INTERVAL_US) {

      uint64_t missed =
          (now - nextSampleTime) /
          SAMPLE_INTERVAL_US;

      nextSampleTime +=
          missed * SAMPLE_INTERVAL_US;
    }

    nextSampleTime +=
        SAMPLE_INTERVAL_US;

    uint64_t sampleTime =
        esp_timer_get_time();

    float current =
        readActivity();

    float fastChange =
        fabs(current - previousActivity);

    previousActivity = current;

    float excess =
        fastChange - changeThreshold;

    if (excess < 0) {
      excess = 0;
    }

    float response =
        scaleResponse(excess);

    if (response < 2.0) {
      response = 0;
    }

    Sample sample;

    sample.timestampUs =
        sampleTime;

    sample.vibration =
        response;

    // Put measurement into buffer.
    // Never wait for network.
    if (xQueueSend(
          sampleQueue,
          &sample,
          0
        ) != pdTRUE) {

      droppedSamples++;
    }
  }
}

// --------------------------------------------------
// NETWORK TASK
//
// This task handles TCP separately.
// If TCP freezes, sensor task keeps running.
// --------------------------------------------------

void networkTask(void* parameter) {

  Sample batch[BATCH_SIZE];

  uint32_t batchNumber = 0;

  while (true) {

    // Collect 50 samples from buffer
    for (int i = 0; i < BATCH_SIZE; i++) {

      xQueueReceive(
        sampleQueue,
        &batch[i],
        portMAX_DELAY
      );
    }

    batchNumber++;

    char packet[2500];

    int pos = snprintf(
      packet,
      sizeof(packet),
      "BATCH,%lu,%d\n",
      (unsigned long)batchNumber,
      BATCH_SIZE
    );

    for (int i = 0; i < BATCH_SIZE; i++) {

      pos += snprintf(
        packet + pos,
        sizeof(packet) - pos,
        "%llu,%.1f\n",
        (unsigned long long)
          batch[i].timestampUs,
        batch[i].vibration
      );
    }

    pos += snprintf(
      packet + pos,
      sizeof(packet) - pos,
      "END\n"
    );

    // Wait for receiver if necessary.
    // Sensor task continues during this.
    while (!connectToMac()) {
      vTaskDelay(
        pdMS_TO_TICKS(1000)
      );
    }

    uint64_t sendStart =
        esp_timer_get_time();

    size_t written =
        client.write(
          (uint8_t*)packet,
          pos
        );

    uint64_t sendEnd =
        esp_timer_get_time();

    if (written != pos) {
      Serial.println(
        "WARNING: incomplete TCP write"
      );

      client.stop();
    }

    int queued =
        uxQueueMessagesWaiting(
          sampleQueue
        );

    Serial.print("batch=");
    Serial.print(batchNumber);

    Serial.print(" send_ms=");
    Serial.print(
      (sendEnd - sendStart) / 1000.0,
      3
    );

    Serial.print(" queued=");
    Serial.print(queued);

    Serial.print(" dropped=");
    Serial.println(droppedSamples);
  }
}

// --------------------------------------------------
// SETUP
// --------------------------------------------------

void setup() {

  Serial.begin(115200);
  delay(1000);

  pinMode(
    VPU_DAT,
    INPUT_PULLDOWN
  );

  if (!ledcAttach(
        VPU_CLK,
        768000,
        2
      )) {
    while (1);
  }

  ledcWrite(
    VPU_CLK,
    2
  );

  delay(500);

  // Wi-Fi
  Serial.println(
    "Connecting to WiFi..."
  );

  WiFi.begin(
    WIFI_SSID,
    WIFI_PASSWORD
  );

  while (
    WiFi.status() != WL_CONNECTED
  ) {
    delay(500);
    Serial.print(".");
  }

  WiFi.setSleep(false);

  Serial.println();
  Serial.println(
    "WiFi connected"
  );

  Serial.print(
    "ESP32 IP: "
  );

  Serial.println(
    WiFi.localIP()
  );

  // Calibration
  Serial.println(
    "Calibrating - DO NOT TOUCH TABLE"
  );

  float changes[CAL_SAMPLES];

  previousActivity =
      readActivity();

  for (
    int i = 0;
    i < CAL_SAMPLES;
    i++
  ) {

    float current =
        readActivity();

    changes[i] =
        fabs(
          current -
          previousActivity
        );

    previousActivity =
        current;
  }

  std::sort(
    changes,
    changes + CAL_SAMPLES
  );

  int index =
      (int)(
        CAL_SAMPLES * 0.995
      );

  if (index >= CAL_SAMPLES) {
    index =
        CAL_SAMPLES - 1;
  }

  changeThreshold =
      changes[index] *
      NOISE_MARGIN;

  if (
    changeThreshold <
    MIN_CHANGE_THRESHOLD
  ) {

    changeThreshold =
        MIN_CHANGE_THRESHOLD;
  }

  previousActivity =
      readActivity();

  Serial.println(
    "Calibration finished"
  );

  // Create ~20 second buffer
  sampleQueue =
      xQueueCreate(
        QUEUE_LENGTH,
        sizeof(Sample)
      );

  if (sampleQueue == NULL) {
    Serial.println(
      "ERROR: Could not create buffer"
    );

    while (1);
  }

  // Higher priority: sensor
  xTaskCreate(
    samplingTask,
    "VPU Sampling",
    4096,
    NULL,
    3,
    NULL
  );

  // Lower priority: networking
  xTaskCreate(
    networkTask,
    "TCP Network",
    8192,
    NULL,
    1,
    NULL
  );

  Serial.println(
    "Sampling and network tasks started"
  );
}

void loop() {
  // Everything happens in the two tasks.
  delay(1000);
}