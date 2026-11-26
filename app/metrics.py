"""Метрики в формате Prometheus: их отдаёт /metrics, а забирает VictoriaMetrics.

Два основных типа метрик:
- Gauge: число, которое может расти и падать (температура, влажность, 0/1).
- Counter: число, которое только растёт (количество ошибок за всё время).

Метки ["sensor"] позволяют вести отдельный ряд значений для каждого датчика:
spa_temperature_celsius{sensor="dht11_1"}.
"""
from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST

# Собственный реестр вместо глобального по умолчанию: в /metrics попадут только
# наши метрики, без служебных python_* и process_*. Поэтому каждая метрика
# ниже создаётся с registry=registry.
registry = CollectorRegistry()

temperature_celsius = Gauge(
    "spa_temperature_celsius", "Sensor temperature reading", ["sensor"], registry=registry
)
humidity_percent = Gauge(
    "spa_humidity_percent", "Sensor humidity reading", ["sensor"], registry=registry
)
illuminance_lux = Gauge(
    "spa_illuminance_lux", "Illuminance reading, lux", ["sensor"], registry=registry
)
thermal_min_celsius = Gauge(
    "spa_thermal_min_celsius", "Coldest pixel of the thermal camera frame", ["sensor"], registry=registry
)
thermal_max_celsius = Gauge(
    "spa_thermal_max_celsius", "Hottest pixel of the thermal camera frame", ["sensor"], registry=registry
)
thermal_mean_celsius = Gauge(
    "spa_thermal_mean_celsius", "Mean temperature over the thermal camera frame", ["sensor"], registry=registry
)
servo_angle_degrees = Gauge(
    "spa_servo_angle_degrees", "Current servo angle in degrees (absent until the first command)", registry=registry
)
sensor_up = Gauge(
    "spa_sensor_up", "1 if the last read from this sensor succeeded, else 0", ["sensor"], registry=registry
)
sensor_last_read_timestamp_seconds = Gauge(
    "spa_sensor_last_read_timestamp_seconds", "Unix timestamp of the last read attempt", ["sensor"], registry=registry
)
# Суффикс _total это соглашение Prometheus для счётчиков.
sensor_read_errors_total = Counter(
    "spa_sensor_read_errors_total", "Total failed read attempts", ["sensor"], registry=registry
)
sensor_reconnects_total = Counter(
    "spa_sensor_reconnects_total", "Total software reconnect attempts of this sensor", ["sensor"], registry=registry
)
# Без меток: сбор включён или выключен для всего приложения сразу.
collection_enabled = Gauge(
    "spa_collection_enabled", "1 if sensor collection is currently enabled, else 0", registry=registry
)


collection_remaining_seconds = Gauge(
    "spa_collection_remaining_seconds",
    "Seconds left in the current measurement started by the button (0 when idle or always_on)",
    registry=registry,
)


# Ключ в словаре, который возвращает Sensor.read(), -> метрика для него.
# Новый вид измерения достаточно добавить сюда и описать Gauge выше.
READINGS = {
    "temperature_celsius": temperature_celsius,
    "humidity_percent": humidity_percent,
    "illuminance_lux": illuminance_lux,
    "thermal_min_celsius": thermal_min_celsius,
    "thermal_max_celsius": thermal_max_celsius,
    "thermal_mean_celsius": thermal_mean_celsius,
}


def clear_readings(sensor: str):
    """Убрать из /metrics значения датчика (при ошибке чтения).

    Тогда в Grafana на графике будет разрыв, а не линия из последнего удачного
    значения. Ряда может не быть (например, датчик ни разу не читался), поэтому
    KeyError от remove() игнорируем.
    """
    for gauge in READINGS.values():
        try:
            gauge.remove(sensor)
        except KeyError:
            pass


def render() -> bytes:
    """Все метрики в текстовом формате Prometheus (то, что видит /metrics)."""
    return generate_latest(registry)
