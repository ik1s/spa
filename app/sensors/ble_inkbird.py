"""Беспроводной термогигрометр Inkbird IBS-TH2 (и IBS-TH1) по Bluetooth Low Energy.

Датчик сам рассылает показания (advertising) несколько раз в минуту; подключаться к нему не
нужно. Приложение слушает эфир через BlueZ (библиотека bleak) в отдельном потоке и хранит
последнюю рассылку от каждого адреса. read() разбирает свежую рассылку нужного датчика.

Формат рассылки (по открытым разборам, напр. inkbird-ble/Theengs; на нашем экземпляре
ещё НЕ проверен): имя устройства "sps", данные производителя 9 байт, если склеить
2 байта «кода производителя» и полезную нагрузку (little-endian):
    int16 температура x100 | uint16 влажность x100 | uint8 щуп (0 встроенный, 1 выносной)
    | uint16 CRC | uint8 заряд батареи, % | uint8 тип
Сырые байты последней рассылки видны в /health (поле raw) — по ним формат сверяется
на реальном датчике.

outdoor: true в config.yaml — датчик на улице: показания идут в метрики spa_outdoor_*
(в отчёте отдельной строкой без оценки по СанПиН; по ним определяется период года).
"""
import asyncio
import logging
import struct
import threading
import time

from .base import Sensor, SensorError

log = logging.getLogger("spa")


def decode_sps(company_id: int, payload: bytes):
    """Разобрать данные производителя IBS-TH1/TH2. None, если формат не подходит."""
    data = company_id.to_bytes(2, "little") + bytes(payload)
    if len(data) != 9:
        return None
    temp, hum, probe, _crc, battery, _kind = struct.unpack("<hHBHBB", data)
    return {
        "temperature_celsius": temp / 100,
        "humidity_percent": hum / 100,
        "battery_percent": battery,
        "external_probe": probe == 1,
    }


class _BleListener:
    """Один на процесс: фоновый поток с asyncio-циклом и BleakScanner."""

    _instance = None
    _instance_lock = threading.Lock()

    @classmethod
    def get(cls):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        try:
            import bleak  # noqa: F401  (проверка, что библиотека есть)
        except ImportError as exc:
            raise SensorError("bleak not installed") from exc
        self._lock = threading.Lock()
        self._latest = {}  # адрес (верхний регистр) -> (время, имя, {код: байты}, rssi)
        self._error = None
        self._thread = threading.Thread(target=self._run, name="ble-listener", daemon=True)
        self._thread.start()

    def _on_advert(self, device, adv):
        if not adv.manufacturer_data:
            return
        with self._lock:
            self._latest[device.address.upper()] = (
                time.time(), adv.local_name, dict(adv.manufacturer_data), adv.rssi,
            )

    def _run(self):
        from bleak import BleakScanner

        async def scan_forever():
            while True:
                try:
                    async with BleakScanner(detection_callback=self._on_advert):
                        self._error = None
                        while True:
                            await asyncio.sleep(3600)
                except Exception as exc:  # адаптер выключен, BlueZ перезапущен и т.п.
                    if str(exc) != self._error:
                        log.warning("BLE scanner error, retrying in 30 s: %s", exc)
                    self._error = str(exc)
                    await asyncio.sleep(30)

        asyncio.run(scan_forever())

    def latest(self, address: str):
        with self._lock:
            return self._latest.get(address.upper())

    @property
    def error(self):
        return self._error


class InkbirdSensor(Sensor):
    def __init__(self, cfg: dict):
        self.name = cfg["name"]
        self.address = cfg["address"].upper()
        self.outdoor = bool(cfg.get("outdoor", False))
        # Рассылка старше этого считается потерянной (датчик вне зоны, села батарейка).
        self.max_age = cfg.get("max_age_seconds", 180)
        self._listener = None

    def read(self) -> dict:
        if self._listener is None:
            self._listener = _BleListener.get()
        entry = self._listener.latest(self.address)
        if entry is None:
            err = self._listener.error
            raise SensorError(f"{self.name}: no BLE advertisement from {self.address} yet"
                              + (f" (scanner: {err})" if err else ""))
        seen, local_name, mdata, rssi = entry
        age = time.time() - seen
        if age > self.max_age:
            raise SensorError(f"{self.name}: last advertisement {age:.0f} s ago (rssi {rssi})")
        values = None
        for company_id, payload in mdata.items():
            values = decode_sps(company_id, payload)
            if values:
                break
        if values is None:
            raw = {hex(k): bytes(v).hex() for k, v in mdata.items()}
            raise SensorError(f"{self.name}: unknown advertisement format from {local_name!r}: {raw}")
        if not (-40 <= values["temperature_celsius"] <= 60 and 0 <= values["humidity_percent"] <= 100):
            raise SensorError(f"{self.name}: implausible values {values}")
        prefix = "outdoor_" if self.outdoor else ""
        return {
            f"{prefix}temperature_celsius": round(values["temperature_celsius"], 2),
            f"{prefix}humidity_percent": round(values["humidity_percent"], 2),
            "battery_percent": values["battery_percent"],
            # Для /health: сырые данные производителя и сила сигнала (метрикой не публикуются).
            "raw": {hex(k): bytes(v).hex() for k, v in mdata.items()},
            "rssi": rssi,
        }
