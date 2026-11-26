"""Поиск BLE-датчиков рядом с хостом: адрес, имя, сила сигнала, данные производителя.

Запуск на хосте (пока приложение тоже слушает эфир, это не мешает):
    /opt/spa/app/venv/bin/python3 /opt/spa/app/tools/ble_scan.py [секунд]
Inkbird IBS-TH1/TH2 обычно называется "sps"; для него печатается разобранная температура.
Адрес нужного датчика вписать в app/config.yaml (address:).
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from bleak import BleakScanner  # noqa: E402

from sensors.ble_inkbird import decode_sps  # noqa: E402


async def main(seconds: float):
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    for address, (device, adv) in sorted(found.items(), key=lambda x: -x[1][1].rssi):
        raw = {hex(k): bytes(v).hex() for k, v in adv.manufacturer_data.items()}
        line = f"{address}  rssi={adv.rssi:4d}  name={adv.local_name!r}  mdata={raw}"
        for k, v in adv.manufacturer_data.items():
            decoded = decode_sps(k, v)
            if decoded and adv.local_name == "sps":
                line += f"  -> {decoded}"
        print(line)


if __name__ == "__main__":
    asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 15))
