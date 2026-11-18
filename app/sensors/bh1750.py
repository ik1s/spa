"""Датчик освещённости BH1750 (модуль GY-302) на шине I2C.

Протокол простой: отправляем команду "измеряй с высоким разрешением" и через
~180 мс читаем два байта. Результат в люксах = число / 1,2.
Адрес 0x23 при выводе ADDR на землю (или никуда), 0x5C при ADDR на питание.
"""
import time

from .base import Sensor, SensorError
from .i2c_bus import open_bus

_POWER_ON = 0x01
_RESET = 0x07
# Одиночное измерение с разрешением 1 лк. После него датчик сам выключается,
# поэтому питание между опросами не расходуется.
_ONE_TIME_HIGH_RES = 0x20
# По даташиту измерение занимает до 180 мс, берём с запасом.
_MEASURE_SECONDS = 0.2


class BH1750Sensor(Sensor):
    def __init__(self, cfg: dict):
        self.name = cfg["name"]
        self.bus_number = cfg.get("bus", 1)
        self.address = cfg.get("address", 0x23)
        self._bus = None

    def reconnect(self):
        """Закрыть шину: следующее чтение откроет её заново."""
        if self._bus is not None:
            try:
                self._bus.close()
            finally:
                self._bus = None

    def read(self) -> dict:
        try:
            if self._bus is None:
                self._bus = open_bus(self.bus_number)
            self._bus.write_byte(self.address, _POWER_ON)
            self._bus.write_byte(self.address, _RESET)
            self._bus.write_byte(self.address, _ONE_TIME_HIGH_RES)
            time.sleep(_MEASURE_SECONDS)
            high, low = self._bus.read_i2c_block_data(self.address, _ONE_TIME_HIGH_RES, 2)
        except OSError as exc:
            # Обрыв провода или неверный адрес: ошибка ввода-вывода шины.
            raise SensorError(f"{self.name}: I2C error at 0x{self.address:02x}: {exc}") from exc
        lux = ((high << 8) | low) / 1.2
        return {"illuminance_lux": round(lux, 1)}
