// VPU chair sensor -> USB serial, for vpu_chair_dashboard.py
//
// Board:  XIAO ESP32-C6 (Seeed), ESP32 Arduino core 3.x
// Wiring: VPU CLK -> D6, VPU DATA -> D7 (same as VPU_HighRate_View)
// Tools menu: USB CDC On Boot -> Enabled   (otherwise nothing appears on USB)
//
// Output, one line every 5 ms (200 Hz), readable by the Arduino Serial Plotter
// and by the dashboard:
//     vibration:37.5   floor:0   ceiling:100
// `vibration` is the largest response seen since the previous line, so short
// impacts (sitting down) are never skipped between prints.
// Lines starting with '#' are status messages; the dashboard ignores them.
//
// Keep the chair still for the ~3 s calibration after reset/power-up.

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

const unsigned long PRINT_US = 5000;  // 200 lines/s

float previousActivity = 0.0;
float changeThreshold = 0.05;
float peakResponse = 0.0;
unsigned long nextPrint = 0;


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
  Serial.println("# VPU chair sensor starting");

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

  // Learn normal background noise (keep the chair still).
  Serial.println("# calibrating, keep the chair still...");
  static float changes[CAL_SAMPLES];
  previousActivity = readActivity();
  for (int i = 0; i < CAL_SAMPLES; i++) {
    float current = readActivity();
    changes[i] = fabs(current - previousActivity);
    previousActivity = current;
  }
  std::sort(changes, changes + CAL_SAMPLES);
  int index = min((int)(CAL_SAMPLES * 0.995), CAL_SAMPLES - 1);
  changeThreshold = max(changes[index] * NOISE_MARGIN, MIN_CHANGE_THRESHOLD);

  Serial.print("# calibrated, change threshold = ");
  Serial.println(changeThreshold, 4);

  previousActivity = readActivity();
  nextPrint = micros();
}


void loop() {
  float current = readActivity();
  float fastChange = fabs(current - previousActivity);
  previousActivity = current;

  float response = scaleResponse(fastChange - changeThreshold);
  if (response < 2.0) {
    response = 0;  // hide tiny background noise
  }
  peakResponse = max(peakResponse, response);

  if ((long)(micros() - nextPrint) >= 0) {
    Serial.print("vibration:");
    Serial.print(peakResponse, 1);
    Serial.println("\tfloor:0\tceiling:100");
    peakResponse = 0;
    nextPrint += PRINT_US;
    // If a read ran long, don't try to catch up with a burst of lines.
    if ((long)(micros() - nextPrint) > (long)PRINT_US) {
      nextPrint = micros() + PRINT_US;
    }
  }
}
