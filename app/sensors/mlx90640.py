"""Тепловизор MLX90640 (матрица 32x24) на шине I2C, адрес 0x33.

Расчёт температур по калибровке из EEPROM датчика берём из драйвера Adafruit
(пакет adafruit-circuitpython-mlx90640, чистый Python). Драйверу нужен объект шины
в стиле CircuitPython (busio.I2C); его обычно даёт пакет Adafruit-Blinka, но Blinka
тянет много лишнего, поэтому здесь своя тонкая обёртка над smbus2 (_SMBusI2C), а
пакеты Adafruit ставятся с --no-deps (см. роль spa_app).

Read() возвращает min/max/mean по кадру, а сам кадр (768 значений, °C) хранится для
эндпоинта /thermal. Расчёт кадра на Python занимает заметное время (около секунды
на Raspberry Pi 5 при 2 Гц: кадр собирается из двух подкадров).
"""
import math
import threading
import time

try:
    from smbus2 import SMBus, i2c_msg
except ImportError:
    SMBus = None


def _stub_typing_modules():
    """Подставить заглушки busio и circuitpython_typing, если Blinka не установлена.

    Драйверы Adafruit импортируют из них имена только для аннотаций типов
    (busio.I2C, ReadableBuffer, WriteableBuffer). Но Python вычисляет аннотации при
    определении функции, поэтому без этих имён импорт adafruit_bus_device падает с
    NameError. Код из заглушек не вызывается: шину драйверу даёт _SMBusI2C ниже.
    """
    import importlib.util
    import sys
    import types

    if importlib.util.find_spec("busio") is None and "busio" not in sys.modules:
        busio = types.ModuleType("busio")
        busio.I2C = object
        sys.modules["busio"] = busio
    if importlib.util.find_spec("circuitpython_typing") is None and "circuitpython_typing" not in sys.modules:
        cpt = types.ModuleType("circuitpython_typing")
        cpt.ReadableBuffer = bytes
        cpt.WriteableBuffer = bytearray
        sys.modules["circuitpython_typing"] = cpt


_stub_typing_modules()
try:
    import adafruit_mlx90640
except ImportError:
    adafruit_mlx90640 = None

from .base import Sensor, SensorError

WIDTH = 32
HEIGHT = 24

# Частоты, которые понимает датчик (Гц) -> код в регистре 0x800D.
_REFRESH_CODES = {0.5: 0, 1: 1, 2: 2, 4: 3, 8: 4, 16: 5}


class _SMBusI2C:
    """Шина I2C с интерфейсом busio.I2C поверх /dev/i2c-N (smbus2)."""

    def __init__(self, bus_number: int):
        self._bus = SMBus(bus_number)
        self._lock = threading.Lock()

    def try_lock(self) -> bool:
        return self._lock.acquire(blocking=False)

    def unlock(self):
        self._lock.release()

    def writeto(self, address, buffer, *, start=0, end=None):
        data = bytes(buffer[start:end])
        if not data:
            # Пустая запись нужна драйверу только для проверки, есть ли устройство.
            # Через smbus2 её надёжнее заменить чтением одного байта.
            self._bus.i2c_rdwr(i2c_msg.read(address, 1))
            return
        self._bus.i2c_rdwr(i2c_msg.write(address, data))

    def readfrom_into(self, address, buffer, *, start=0, end=None):
        end = len(buffer) if end is None else end
        msg = i2c_msg.read(address, end - start)
        self._bus.i2c_rdwr(msg)
        buffer[start:end] = bytes(msg)

    def writeto_then_readfrom(self, address, out_buffer, in_buffer, *, out_start=0, out_end=None, in_start=0, in_end=None):
        in_end = len(in_buffer) if in_end is None else in_end
        write = i2c_msg.write(address, bytes(out_buffer[out_start:out_end]))
        read = i2c_msg.read(address, in_end - in_start)
        # Запись адреса регистра и чтение одной транзакцией (повторный START),
        # как требует протокол MLX90640.
        self._bus.i2c_rdwr(write, read)
        in_buffer[in_start:in_end] = bytes(read)

    def close(self):
        self._bus.close()


if adafruit_mlx90640 is not None:

    class _MLX90640(adafruit_mlx90640.MLX90640):
        """Драйвер Adafruit с таймаутом ожидания кадра.

        В оригинальном _GetFrameData ожидание готовности подкадра идёт в бесконечном
        цикле без паузы: если датчик перестанет отвечать новыми данными, поток опроса
        зависнет навсегда. Здесь та же логика, но с паузой и ограничением по времени.
        """

        frame_timeout = 3.0

        def _GetFrameData(self, frameData):
            statusRegister = [0]
            controlRegister = [0]
            deadline = time.monotonic() + self.frame_timeout
            while True:
                self._I2CReadWords(0x8000, statusRegister)
                if statusRegister[0] & 0x0008:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError("timeout waiting for a new subpage")
                time.sleep(0.01)

            cnt = 0
            dataReady = 1
            while dataReady != 0 and cnt < 5:
                self._I2CWriteWord(0x8000, 0x0030)
                self._I2CReadWords(0x0400, frameData, end=832)
                self._I2CReadWords(0x8000, statusRegister)
                dataReady = statusRegister[0] & 0x0008
                cnt += 1
            if cnt > 4:
                raise RuntimeError("too many retries reading a subpage")

            self._I2CReadWords(0x800D, controlRegister)
            frameData[832] = controlRegister[0]
            frameData[833] = statusRegister[0] & 0x0001
            return frameData[833]


class MLX90640Sensor(Sensor):
    def __init__(self, cfg: dict):
        self.name = cfg["name"]
        self.bus_number = cfg.get("bus", 1)
        self.address = cfg.get("address", 0x33)
        self.refresh_hz = cfg.get("refresh_hz", 2)
        if self.refresh_hz not in _REFRESH_CODES:
            raise ValueError(f"{self.name}: refresh_hz must be one of {sorted(_REFRESH_CODES)}")
        self._i2c = None
        self._dev = None
        # Последний удачный кадр для /thermal. Пишет поток опроса, читают HTTP-потоки.
        self._frame_lock = threading.Lock()
        self._frame = None

    def _connect(self):
        if SMBus is None:
            raise SensorError("smbus2 not installed")
        if adafruit_mlx90640 is None:
            raise SensorError("adafruit-circuitpython-mlx90640 not installed")
        try:
            self._i2c = _SMBusI2C(self.bus_number)
            # Конструктор читает EEPROM (832 слова) и разбирает калибровку.
            self._dev = _MLX90640(self._i2c, self.address)
            self._dev.refresh_rate = _REFRESH_CODES[self.refresh_hz]
        except (OSError, ValueError, RuntimeError) as exc:
            self.reconnect()
            raise SensorError(f"{self.name}: cannot init MLX90640 at 0x{self.address:02x}: {exc}") from exc

    def reconnect(self):
        """Закрыть шину: следующее чтение заново прочитает EEPROM и настроит датчик."""
        self._dev = None
        if self._i2c is not None:
            try:
                self._i2c.close()
            finally:
                self._i2c = None

    def read(self) -> dict:
        if self._dev is None:
            self._connect()
        pixels = [0.0] * (WIDTH * HEIGHT)
        try:
            self._dev.getFrame(pixels)
        except (OSError, RuntimeError, ValueError) as exc:
            raise SensorError(f"{self.name}: frame read failed: {exc}") from exc

        valid = [p for p in pixels if math.isfinite(p)]
        if len(valid) < len(pixels) // 2:
            raise SensorError(f"{self.name}: only {len(valid)} of {len(pixels)} pixels are valid")
        stats = {
            "thermal_min_celsius": round(min(valid), 2),
            "thermal_max_celsius": round(max(valid), 2),
            "thermal_mean_celsius": round(sum(valid) / len(valid), 2),
        }
        frame = {
            "sensor": self.name,
            "timestamp": time.time(),
            "width": WIDTH,
            "height": HEIGHT,
            **stats,
            # 24 строки по 32 значения в порядке памяти датчика (строка 0 первая); какая
            # сторона кадра это верх в помещении, зависит от того, как закреплён датчик.
            # null для битых пикселей.
            "pixels": [
                [round(p, 2) if math.isfinite(p) else None for p in pixels[row * WIDTH:(row + 1) * WIDTH]]
                for row in range(HEIGHT)
            ],
        }
        with self._frame_lock:
            self._frame = frame
        return stats

    def last_frame(self):
        """Последний удачный кадр (словарь для /thermal) или None, если кадров ещё не было."""
        with self._frame_lock:
            return self._frame
