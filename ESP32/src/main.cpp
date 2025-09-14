#include <WiFi.h>
#include <PubSubClient.h>

const char* ssid = "Zenet";
const char* password = "20a06a1987";
const char* mqtt_broker = "broker.hivemq.com";
const int mqtt_port = 1883;
const char* device_id = "device1";
char mqtt_topic[50];
// --- END CONFIGURATION ---

const int trigPin = 13;
const int echoPin = 12;
float detectionThresholdCM = 100.0; // Valore da calibrare
long measurementInterval = 1000;

WiFiClient wifiClient;
PubSubClient mqttClient(wifiClient);
String last_state = "";

// Function declarations
float getDistanceCM();
void reconnect_mqtt();
void publish_current_state();

void setup() {
  Serial.begin(115200);
  delay(10);
  snprintf(mqtt_topic, sizeof(mqtt_topic), "esp32/%s/sensor", device_id);

  Serial.println();
  Serial.print("Connecting to ");
  Serial.println(ssid);
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi connected!");
  Serial.print("IP address: ");
  Serial.println(WiFi.localIP());

  mqttClient.setServer(mqtt_broker, mqtt_port);
  pinMode(trigPin, OUTPUT);
  pinMode(echoPin, INPUT);
  Serial.println("Ultrasonic sensor ready.");
}

void loop() {
  if (!mqttClient.connected()) {
    reconnect_mqtt();
  }
  mqttClient.loop();

  // Get distance reading
  float distance = getDistanceCM();

  // *** NEW: Detailed debug output for calibration ***
  // This will print the exact distance every second.
  Serial.print("Distance reading: ");
  Serial.print(distance);
  Serial.println(" cm");
  // *** END of debug output ***

  // Determine current state based on the reading
  String current_state;
  if (distance > 0 && distance < detectionThresholdCM) {
    current_state = "detected";
  } else {
    current_state = "clear";
  }

  // Only publish if the state has actually changed during the loop
  if (current_state != last_state) {
    Serial.print("State changed! New state: ");
    Serial.println(current_state);
    if (mqttClient.publish(mqtt_topic, current_state.c_str())) {
        Serial.println("MQTT message published successfully.");
        last_state = current_state; 
    } else {
        Serial.println("MQTT publish failed.");
    }
  }

  delay(measurementInterval);
}

void reconnect_mqtt() {
  while (!mqttClient.connected()) {
    Serial.print("Attempting MQTT connection...");
    if (mqttClient.connect("esp32-bathroom-sensor-client")) {
      Serial.println("connected!");
      publish_current_state();
    } else {
      Serial.print("failed, rc=");
      Serial.print(mqttClient.state());
      Serial.println(" try again in 5 seconds");
      delay(5000);
    }
  }
}

void publish_current_state() {
  String current_state;
  float distance = getDistanceCM();
  if (distance > 0 && distance < detectionThresholdCM) {
    current_state = "detected";
  } else {
    current_state = "clear";
  }
  
  Serial.print("Publishing initial/reconnect state: ");
  Serial.println(current_state);

  if (mqttClient.publish(mqtt_topic, current_state.c_str())) {
      Serial.println("Initial state published successfully.");
      last_state = current_state;
  } else {
      Serial.println("Initial state publish failed.");
  }
}

float getDistanceCM() {
  digitalWrite(trigPin, LOW);
  delayMicroseconds(2);
  digitalWrite(trigPin, HIGH);
  delayMicroseconds(10);
  digitalWrite(trigPin, LOW);

  long duration = pulseIn(echoPin, HIGH, 30000);

  if (duration == 0) {
    return -1.0;
  }

  float distance = duration / 58.2;
  return distance;
}