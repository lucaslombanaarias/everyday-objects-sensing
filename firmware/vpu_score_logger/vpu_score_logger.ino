// VPU score logger -> USB serial, one line per measurement window
//
// Board:  XIAO ESP32-C6 (Seeed), ESP32 Arduino core 3.x
// Wiring: VPU CLK -> D6, VPU DATA -> D7 (same as firmware/vpu_lab_demo)
// Tools menu: USB CDC On Boot -> Enabled   (otherwise nothing appears on USB)
//
// Copy of firmware/vpu_lab_demo/VPU_Chair_Serial with output changes only: the
// 768 kHz clock, the 5,000-read window, the calibration and the score are unchanged.
// Every window is printed (no 5 ms schedule, no peak hold), one line each:
//     n:1234   t_us:5678901   ones:2513   vibration:0.0   thr:0.0412
// n          window counter, +1 per window; a gap on the host means dropped lines
// t_us       micros() at the start of the window (32-bit, wraps every 2^32 us)
// ones       DATA reads that were high, out of N
// vibration  activity score 0 to 100, same as VPU_Chair_Serial but per window
// thr        calibrated noise threshold (percentage points of duty cycle)
// Lines starting with '#' are status messages; dashboards and parsers skip them.
//
// Keep the sensor still for the calibration after reset/power-up.

#include <Arduino.h>
#include <math.h>
#include <algorithm>

#define VPU_CLK D6
#define VPU_DAT D7

// Same sensor reading size that has been working well.
const int N = 5000;

// Startup calibration
const int CAL_SAMPLES = 600;

const float NOISE_MARGIN = 1.15;
const float MIN_CHANGE_THRESHOLD = 0.030;

// Same general display scaling.
const float DISPLAY_RANGE_MULTIPLIER = 18.0;

float previousActivity = 0.0;
float changeThreshold = 0.05;
unsigned long windowCount = 0;


int readOnes() {
  int highs = 0;
  for (int i = 0; i < N; i++) {
    if (digitalRead(VPU_DAT)) {
      highs++;
    }
  }
  return highs;
}


float toActivity(int highs) {
  return 100.0f * highs / N;
}


float scaleResponse(float excess) {
  if (excess <= 0) {
    return 0;
  }
  float relative = excess / changeThreshold;
  float score = 100.0f * log1pf(relative) / log1pf(DISPLAY_RANGE_MULTIPLIER);
  return constrain(score, 0.0f, 100.0f);
}


void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("# VPU score logger starting");

  pinMode(VPU_DAT, INPUT_PULLDOWN);

  // 768 kHz clock for the VPU on D6.
  if (!ledcAttach(VPU_CLK, 768000, 2)) {
    while (1) {
      Serial.println("# ERROR: could not start VPU clock on D6");
      delay(1000);
    }
  }
  ledcWrite(VPU_CLK, 2);
  delay(500);

  // Learn normal background noise (keep the sensor still).
  Serial.println("# calibrating, keep the sensor still...");
  static float changes[CAL_SAMPLES];
  previousActivity = toActivity(readOnes());
  unsigned long calStart = micros();
  for (int i = 0; i < CAL_SAMPLES; i++) {
    float current = toActivity(readOnes());
    changes[i] = fabs(current - previousActivity);
    previousActivity = current;
  }
  unsigned long calUs = micros() - calStart;
  std::sort(changes, changes + CAL_SAMPLES);
  int index = min((int)(CAL_SAMPLES * 0.995), CAL_SAMPLES - 1);
  changeThreshold = max(changes[index] * NOISE_MARGIN, MIN_CHANGE_THRESHOLD);

  // Measured on this board: mean duration of one N-read window during calibration.
  Serial.print("# window_us_mean = ");
  Serial.println((float)calUs / CAL_SAMPLES, 2);
  Serial.print("# reads_per_window = ");
  Serial.println(N);
  Serial.print("# calibrated, change threshold = ");
  Serial.println(changeThreshold, 4);
  Serial.println("# fields: n t_us ones vibration thr");

  previousActivity = toActivity(readOnes());
}


void loop() {
  unsigned long start = micros();
  int ones = readOnes();
  float current = toActivity(ones);
  float fastChange = fabs(current - previousActivity);
  previousActivity = current;

  float response = scaleResponse(fastChange - changeThreshold);
  if (response < 2.0) {
    response = 0;  // hide tiny background noise
  }

  char line[96];
  int len = snprintf(line, sizeof(line), "n:%lu\tt_us:%lu\tones:%d\tvibration:%.1f\tthr:%.4f\n",
                     windowCount, start, ones, response, changeThreshold);
  Serial.write((const uint8_t*)line, len);
  windowCount++;
}
