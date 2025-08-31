
import threading
import paho.mqtt.client as mqtt
from typing import Callable, Optional
from . import settings

class MQTTBridge:
    """
    A small wrapper around paho-mqtt that:
    - connects to broker
    - subscribes to SENSOR_TOPIC
    - exposes publish_command(command: str)
    - calls a callback on sensor message
    """
    def __init__(self):
        self._client = mqtt.Client()
        if settings.MQTT_USERNAME:
            self._client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD or None)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._sensor_callback: Optional[Callable[[str], None]] = None

    def set_sensor_callback(self, cb: Callable[[str], None]):
        self._sensor_callback = cb

    def _on_connect(self, client, userdata, flags, rc):
        print(f"[MQTT] Connected with result code {rc}")
        client.subscribe(settings.SENSOR_TOPIC, qos=1)
        print(f"[MQTT] Subscribed to {settings.SENSOR_TOPIC}")

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", errors="ignore")
        print(f"[MQTT] {msg.topic} -> {payload}")
        if self._sensor_callback:
            try:
                self._sensor_callback(payload)
            except Exception as e:
                print(f"[MQTT] sensor_callback error: {e}")

    def start(self):
        self._client.connect(settings.MQTT_BROKER, settings.MQTT_PORT, keepalive=60)
        # run network loop in background thread
        self._client.loop_start()
        print("[MQTT] loop_start")

    def stop(self):
        try:
            self._client.loop_stop()
        except Exception:
            pass
        try:
            self._client.disconnect()
        except Exception:
            pass

    def publish_command(self, command: str):
        print(f"[MQTT] publish -> {settings.COMMAND_TOPIC}: {command}")
        # qos=1 to ensure delivery at least once
        self._client.publish(settings.COMMAND_TOPIC, command, qos=1, retain=False)
