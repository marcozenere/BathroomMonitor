#include <WiFi.h>

const char* ssid = "Zenet";
const char* password = "20a06a1987";

// Ultrasonic sensor pins
const int trigPin = 13;   // GPIO for Trigger
const int echoPin = 12;  // GPIO for Echo

// Configuration parameters
float maxDistanceCM = 200.0; // maximum distance in cm to detect
float detectionThresholdCM = 100.0; // distance below which we consider "detected"

// Function declaration (avoids scope issue)
float getDistanceCM();

void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println();
  Serial.print("Connecting to ");
  Serial.println(ssid);

  WiFi.begin(ssid, password);

  // Wait until connected
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println();
  Serial.println("WiFi connected!");
  Serial.print("IP address: ");
  Serial.println(WiFi.localIP());

  pinMode(trigPin, OUTPUT);
  pinMode(echoPin, INPUT);

  Serial.println("Ultrasonic sensor ready...");
}

void loop() {
  float distance = getDistanceCM();

  Serial.print("Distance: ");
  Serial.print(distance);
  Serial.println(" cm");

  if (distance > 0 && distance < detectionThresholdCM) {
    Serial.println("Object detected within threshold!");
    delay(500); // measurement interval
  }else{
    delay(1000);
  }
}

// Function definition
float getDistanceCM() {
  // Clear trigger pin
  digitalWrite(trigPin, LOW);
  delayMicroseconds(2);

  // Send 10µs HIGH pulse to trigger
  digitalWrite(trigPin, HIGH);
  delayMicroseconds(10);
  digitalWrite(trigPin, LOW);

  // Measure echo duration (in microseconds)
  long duration = pulseIn(echoPin, HIGH, maxDistanceCM * 58 * 2); 
  if (duration == 0) {
    return -1; // No reading (timeout)
  }

  // Convert to centimeters
  float distance = duration / 58.0; // 58 µs per cm (round trip)
  return distance;
}