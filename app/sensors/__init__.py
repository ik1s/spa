from .base import Sensor, SensorError
from .bh1750 import BH1750Sensor
from .ble_inkbird import InkbirdSensor
from .dht11 import DHT11Sensor
from .mlx90640 import MLX90640Sensor

# Реестр датчиков: значение поля "type" из config.yaml -> класс датчика.
# Так main.py создаёт нужный датчик по имени типа и ничего не знает о конкретных
# классах. Новый тип датчика достаточно добавить сюда одной строкой.
SENSOR_TYPES = {
    "dht11": DHT11Sensor,
    "bh1750": BH1750Sensor,
    "mlx90640": MLX90640Sensor,
    "inkbird_ibs_th": InkbirdSensor,
}


def create_sensor(cfg: dict) -> Sensor:
    """Создать датчик по его описанию из config.yaml (фабричная функция)."""
    sensor_type = cfg["type"]
    try:
        cls = SENSOR_TYPES[sensor_type]
    except KeyError:
        # Опечатка в конфиге должна приводить к понятной ошибке при старте,
        # а не к странному поведению позже.
        raise ValueError(f"unknown sensor type: {sensor_type!r}")
    return cls(cfg)
