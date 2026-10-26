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
# Без меток: сбор включён или выключен для всего приложения сразу.
collection_enabled = Gauge(
    "spa_collection_enabled", "1 if sensor collection is currently enabled, else 0", registry=registry
)


def render() -> bytes:
    """Все метрики в текстовом формате Prometheus (то, что видит /metrics)."""
    return generate_latest(registry)
