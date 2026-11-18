"""Общий доступ к шине I2C для датчиков (сейчас BH1750).

Модуль smbus2 ставится в venv через pip. На обычном компьютере без шины I2C
импорт и открытие шины могут не получиться: тогда датчик сообщит SensorError,
а приложение продолжит работать (так же, как с RPi.GPIO у DHT11).
"""
try:
    from smbus2 import SMBus
except ImportError:
    SMBus = None

from .base import SensorError


def open_bus(bus_number: int):
    """Открыть шину I2C (на Raspberry Pi 5 это /dev/i2c-1, bus_number: 1)."""
    if SMBus is None:
        raise SensorError("smbus2 not installed")
    try:
        return SMBus(bus_number)
    except OSError as exc:
        # Обычно: шина не включена (dtparam=i2c_arm=on) или нет прав (группа i2c).
        raise SensorError(f"cannot open /dev/i2c-{bus_number}: {exc}") from exc
