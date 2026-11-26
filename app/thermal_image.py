"""Картинки и снимки кадров тепловизора.

- render_png(frame): кадр 32x24 из MLX90640Sensor.last_frame() -> PNG с цветовой
  шкалой (тёмно-фиолетовый = самый холодный пиксель кадра, светло-жёлтый = самый
  горячий). Шкала своя у каждого кадра, поэтому цвета разных кадров не сравнимы:
  абсолютные значения смотрите в JSON или в метриках min/max.
- SnapshotStore: папка со снимками (PNG + JSON с температурами), хранит последние N.

PNG собирается вручную (zlib + struct), без Pillow: это несколько десятков строк,
а лишняя зависимость в venv на Raspberry Pi не нужна.
"""
import json
import logging
import struct
import zlib
from datetime import datetime
from pathlib import Path

log = logging.getLogger("spa")

# Опорные цвета шкалы (похоже на палитру inferno): от холодного к горячему.
_PALETTE = [
    (0, 0, 4), (40, 11, 84), (101, 21, 110), (159, 42, 99),
    (212, 72, 66), (245, 125, 21), (250, 193, 39), (252, 255, 164),
]
# Цвет битого пикселя (null в кадре).
_BAD_PIXEL = (128, 128, 128)


def _color(t: float):
    """Цвет для t от 0 (холодно) до 1 (горячо): линейно между опорными цветами."""
    t = min(max(t, 0.0), 1.0) * (len(_PALETTE) - 1)
    i = min(int(t), len(_PALETTE) - 2)
    f = t - i
    a, b = _PALETTE[i], _PALETTE[i + 1]
    return tuple(round(a[k] + (b[k] - a[k]) * f) for k in range(3))


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def render_png(frame: dict, scale: int = 10) -> bytes:
    """Кадр -> PNG размером (32*scale) x (24*scale), каждый пиксель датчика это квадрат scale x scale."""
    pixels = frame["pixels"]
    valid = [v for row in pixels for v in row if v is not None]
    lo, hi = (min(valid), max(valid)) if valid else (0.0, 1.0)
    span = (hi - lo) or 1.0

    raw = bytearray()
    for row in pixels:
        line = bytearray()
        for v in row:
            rgb = _BAD_PIXEL if v is None else _color((v - lo) / span)
            line += bytes(rgb) * scale
        # Каждая строка PNG начинается с байта типа фильтра (0 = без фильтра).
        raw += (b"\x00" + bytes(line)) * scale

    width, height = len(pixels[0]) * scale, len(pixels) * scale
    # IHDR: ширина, высота, 8 бит на канал, тип цвета 2 (RGB), сжатие, фильтр, без чересстрочности.
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _png_chunk(b"IEND", b"")
    )


class SnapshotStore:
    """Папка со снимками тепловизора: thermal_ГГГГММДД_ЧЧММСС.png и .json, последние keep штук."""

    def __init__(self, directory: str, keep: int = 100):
        self.dir = Path(directory)
        self.keep = keep

    def save(self, frame: dict):
        """Сохранить кадр и удалить самые старые снимки сверх keep. Возвращает путь к PNG или None."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.fromtimestamp(frame["timestamp"]).strftime("%Y%m%d_%H%M%S")
            base = self.dir / f"thermal_{stamp}"
            png = base.with_suffix(".png")
            # Сначала во временный файл, потом переименование: недописанный файл не попадёт в папку.
            for path, data in ((png, render_png(frame)), (base.with_suffix(".json"), json.dumps(frame).encode())):
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_bytes(data)
                tmp.replace(path)
            self._rotate()
            return png
        except OSError as exc:
            # Нет прав или места на диске: снимок пропускаем, замер продолжается.
            log.warning("cannot save thermal snapshot to %s: %s", self.dir, exc)
            return None

    def _rotate(self):
        # Имена с датой и временем сортируются по возрасту как строки.
        snapshots = sorted(self.dir.glob("thermal_*.png"))
        for old in snapshots[: max(0, len(snapshots) - self.keep)]:
            for path in (old, old.with_suffix(".json")):
                path.unlink(missing_ok=True)


class PngCache:
    """Последняя отрисованная картинка: /thermal.png не перерисовывает тот же кадр на каждый запрос."""

    def __init__(self):
        self._key = None
        self._png = None

    def get(self, frame: dict) -> bytes:
        key = (frame["sensor"], frame["timestamp"])
        if key != self._key:
            self._png, self._key = render_png(frame), key
        return self._png

