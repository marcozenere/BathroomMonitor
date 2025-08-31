
# ESP32 ↔ Telegram (Mini App/Bot) via MQTT Bridge (Render-ready)

Single Render web service that hosts:
- FastAPI backend
- MQTT bridge (paho-mqtt)
- Telegram bot (python-telegram-bot)

## Deploy on Render

1. Push this folder to a GitHub repo.
2. Create a **Web Service** on Render pointing to the repo.
3. Build Command:
   ```
   pip install -r requirements.txt
   ```
4. Start Command:
   ```
   uvicorn app.main:app --host 0.0.0.0 --port $PORT
   ```
5. Set Environment Variables (Settings → Environment):
   - `TELEGRAM_BOT_TOKEN` (required)
   - `TELEGRAM_CHAT_ID` (optional, used for alerts)
   - `MQTT_BROKER` (default: broker.hivemq.com)
   - `MQTT_PORT` (default: 1883)
   - `DEVICE_ID` (default: device1)
   - `ALERT_DISTANCE_CM` (default: 50)

## MQTT Topics

- Sensor publishes to: `esp32/<DEVICE_ID>/sensor`
- Backend publishes commands to: `esp32/<DEVICE_ID>/commands`

## Test Commands (Telegram)

- `/start`
- `/ping`
- `/led_on`
- `/led_off`

## HTTP Endpoint (optional for Mini App)

- `POST /send_command` with JSON `{"command": "led_on"}`

## ESP32 Payload Format

Publish either a raw number (e.g., `42.5`) or JSON: `{"distance": 42.5}`
