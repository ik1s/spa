"""Картинки и снимки кадров тепловизора.

- render_png(frame): кадр 32x24 из MLX90640Sensor.last_frame() -> PNG с цветовой
  шкалой (тёмно-фиолетовый = самый холодный пиксель кадра, светло-жёлтый = самый
  горячий). Шкала своя у каждого кадра, поэтому цвета разных кадров не сравнимы:
  абсолютные значения смотрите в JSON или в метриках min/max.
- SnapshotStore: снимки по замерам (подпапка на замер, PNG + JSON), хранит последние N замеров.

PNG собирается вручную (zlib + struct), без Pillow: это несколько десятков строк,
а лишняя зависимость в venv на Raspberry Pi не нужна.
"""
import json
import logging
import shutil
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


def render_png(frame: dict, scale: int = 10, marks=None) -> bytes:
    """Кадр -> PNG размером (32*scale) x (24*scale), каждый пиксель датчика это квадрат scale x scale.

    marks: {(строка, столбец): (r, g, b)} — обвести эти пиксели датчика контуром заданного
    цвета (так на снимке отмечаются холодные зоны). Контур рисуется по внешней границе зоны:
    сторона пикселя закрашивается, если соседний пиксель не отмечен тем же цветом.
    """
    pixels = frame["pixels"]
    rows, cols = len(pixels), len(pixels[0])
    valid = [v for row in pixels for v in row if v is not None]
    lo, hi = (min(valid), max(valid)) if valid else (0.0, 1.0)
    span = (hi - lo) or 1.0
    marks = marks or {}
    edge = max(1, scale // 5)  # толщина контура в пикселях картинки

    raw = bytearray()
    for r, row in enumerate(pixels):
        base = [_BAD_PIXEL if v is None else _color((v - lo) / span) for v in row]
        for y in range(scale):
            line = bytearray(b"\x00")  # байт типа фильтра PNG (0 = без фильтра) в начале строки
            for c in range(cols):
                rgb = base[c]
                mark = marks.get((r, c))
                if mark is None:
                    line += bytes(rgb) * scale
                    continue
                for x in range(scale):
                    color = rgb
                    if mark is not None and (
                        (y < edge and marks.get((r - 1, c)) != mark)
                        or (y >= scale - edge and marks.get((r + 1, c)) != mark)
                        or (x < edge and marks.get((r, c - 1)) != mark)
                        or (x >= scale - edge and marks.get((r, c + 1)) != mark)
                    ):
                        color = mark
                    line += bytes(color)
            raw += line

    width, height = cols * scale, rows * scale
    # IHDR: ширина, высота, 8 бит на канал, тип цвета 2 (RGB), сжатие, фильтр, без чересстрочности.
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _png_chunk(b"IEND", b"")
    )


class SnapshotStore:
    """Снимки тепловизора по замерам: <dir>/<id замера>/thermal_ГГГГММДД_ЧЧММСС.png и .json.

    Хранятся снимки последних keep замеров (подпапок), старые подпапки удаляются целиком.
    """

    def __init__(self, directory: str, keep: int = 30):
        self.dir = Path(directory)
        self.keep = keep

    def session_dir(self, session: str) -> Path:
        return self.dir / session

    def save(self, frame: dict, session: str):
        """Сохранить кадр в подпапку замера. Возвращает путь к PNG или None при ошибке записи."""
        try:
            folder = self.session_dir(session)
            new_session = not folder.exists()
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.fromtimestamp(frame["timestamp"]).strftime("%Y%m%d_%H%M%S")
            base = folder / f"thermal_{stamp}"
            png = base.with_suffix(".png")
            # Сначала во временный файл, потом переименование: недописанный файл не попадёт в папку.
            for path, data in ((png, render_png(frame)), (base.with_suffix(".json"), json.dumps(frame).encode())):
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_bytes(data)
                tmp.replace(path)
            if new_session:
                self._rotate()
            return png
        except OSError as exc:
            # Нет прав или места на диске: снимок пропускаем, замер продолжается.
            log.warning("cannot save thermal snapshot to %s: %s", self.dir, exc)
            return None

    def snapshots(self, session: str):
        """Имена PNG-снимков замера по времени."""
        folder = self.session_dir(session)
        return sorted(p.name for p in folder.glob("thermal_*.png")) if folder.is_dir() else []

    def load(self, session: str, name: str):
        """Кадр (JSON рядом с PNG) или None."""
        try:
            return json.loads((self.session_dir(session) / name).with_suffix(".json").read_text())
        except (OSError, ValueError):
            return None

    def _rotate(self):
        # Имена подпапок это время начала замера: сортируются по возрасту как строки.
        sessions = sorted(p for p in self.dir.iterdir() if p.is_dir())
        for old in sessions[: max(0, len(sessions) - self.keep)]:
            shutil.rmtree(old, ignore_errors=True)
            log.info("thermal snapshots of old measurement %s removed", old.name)


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

