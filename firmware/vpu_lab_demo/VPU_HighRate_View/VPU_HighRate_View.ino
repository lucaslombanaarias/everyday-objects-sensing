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

// Try to display about 200 times/sec instead of 50.
const unsigned long PLOT_MS = 5;

float previousActivity = 0.0;
float changeThreshold = 0.05;

unsigned long lastPlot = 0;


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

  float relative =
      excess / changeThreshold;

  float score =
      100.0f *
      log1pf(relative) /
      log1pf(DISPLAY_RANGE_MULTIPLIER);

  if (score > 100) {
    score = 100;
  }

  if (score < 0) {
    score = 0;
  }

  return score;
}


void setup() {

  Serial.begin(115200);
  delay(1000);

  pinMode(VPU_DAT, INPUT_PULLDOWN);

  if (!ledcAttach(VPU_CLK, 768000, 2)) {
    while (1);
  }

  ledcWrite(VPU_CLK, 2);

  delay(500);


  // Learn normal background noise.
  float changes[CAL_SAMPLES];

  previousActivity = readActivity();

  for (int i = 0; i < CAL_SAMPLES; i++) {

    float current = readActivity();

    changes[i] =
        fabs(current - previousActivity);

    previousActivity = current;
  }


  std::sort(
      changes,
      changes + CAL_SAMPLES
  );


  int index =
      (int)(CAL_SAMPLES * 0.995);

  if (index >= CAL_SAMPLES) {
    index = CAL_SAMPLES - 1;
  }


  changeThreshold =
      changes[index] *
      NOISE_MARGIN;

  if (changeThreshold <
      MIN_CHANGE_THRESHOLD) {

    changeThreshold =
        MIN_CHANGE_THRESHOLD;
  }


  previousActivity = readActivity();

  lastPlot = millis();
}


void loop() {

  unsigned long now = millis();

  float current = readActivity();

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


  // Hide tiny background noise.
  if (response < 2.0) {
    response = 0;
  }


  if (now - lastPlot >= PLOT_MS) {

    Serial.print("vibration:");
    Serial.print(response, 1);

    Serial.print("\tfloor:0");
    Serial.println("\tceiling:100");

    lastPlot = now;
  }
}