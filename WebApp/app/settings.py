
import os

# Telegram
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # optional default chat for alerts

# MQTT
MQTT_BROKER = os.getenv("MQTT_BROKER", "broker.hivemq.com")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")

# Topics (you can parameterize the device id if you have multiple boards)
DEVICE_ID = os.getenv("DEVICE_ID", "device1")
SENSOR_TOPIC = os.getenv("SENSOR_TOPIC", f"esp32/{DEVICE_ID}/sensor")
COMMAND_TOPIC = os.getenv("COMMAND_TOPIC", f"esp32/{DEVICE_ID}/commands")

# Business logic thresholds
ALERT_DISTANCE_CM = float(os.getenv("ALERT_DISTANCE_CM", "50"))
