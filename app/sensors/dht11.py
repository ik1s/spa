"""Датчик температуры и влажности DHT11 на GPIO Raspberry Pi.

У DHT11 один сигнальный провод и свой протокол, поэтому готовой библиотеки тут
нет, и мы читаем импульсы вручную (это называется bit-banging):

1. Мы прижимаем линию к земле (LOW) на 20 мс. Это сигнал датчику "начинай".
2. Отпускаем линию и переключаем пин на вход. Резистор подтяжки (pull-up) сам
   возвращает линию в HIGH.
3. Датчик отвечает серией импульсов: сначала служебный ответ, потом 40 бит данных.
4. Каждый бит кодируется длиной HIGH-импульса: короткий (~27 мкс) это 0,
   длинный (~70 мкс) это 1.
5. 40 бит это 5 байт: влажность (целая и дробная часть), температура
   (целая и дробная часть) и контрольная сумма.

Python не работает в реальном времени, поэтому тайминги получаются неидеальными.
Иногда контрольная сумма не сходится. Это нормально: ошибка попадает в /health,
а следующий опрос обычно проходит успешно.
"""
import time

from .base import Sensor, SensorError

# На Raspberry Pi 5 модуль RPi.GPIO даёт системный пакет python3-rpi-lgpio.
# На обычном компьютере (без Raspberry Pi) его нет. Чтобы приложение всё равно
# запускалось и его можно было отлаживать, импорт необязательный: read() тогда
# просто сообщит SensorError.
try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

# Сколько секунд слушаем линию после стартового сигнала. Ответа датчика
# (около 4 мс) хватает с большим запасом.
_READ_WINDOW_SECONDS = 0.2

# Граница между битами в микросекундах: HIGH-импульс короче порога это 0,
# длиннее это 1.
_BIT_THRESHOLD_MICROS = 50


class DHT11Sensor(Sensor):
    def __init__(self, cfg: dict):
        self.name = cfg["name"]
        # Номер пина по нумерации BCM (номер GPIO, а не номер ножки на гребёнке).
        # Например, pin: 4 это GPIO4.
        self.pin = cfg["pin"]
        if GPIO is not None:
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)

    def _capture_transitions(self):
        """Послать датчику стартовый сигнал и записать все смены уровня линии.

        Возвращает список пар (уровень, время), где время взято у perf_counter()
        в секундах.
        """
        # Стартовый сигнал: LOW на 20 мс.
        GPIO.setup(self.pin, GPIO.OUT)
        GPIO.output(self.pin, GPIO.LOW)
        time.sleep(0.02)
        # Отпускаем линию и слушаем ответ. PUD_UP включает внутреннюю подтяжку
        # к питанию, чтобы в простое линия была в HIGH.
        GPIO.output(self.pin, GPIO.HIGH)
        GPIO.setup(self.pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        transitions = []
        last = GPIO.input(self.pin)
        start = time.perf_counter()
        # Занятый цикл (busy loop): как можно быстрее опрашиваем пин и
        # запоминаем момент каждой смены уровня.
        while time.perf_counter() - start < _READ_WINDOW_SECONDS:
            current = GPIO.input(self.pin)
            now = time.perf_counter()
            if current != last:
                transitions.append((current, now))
                last = current
        return transitions

    @staticmethod
    def _decode(transitions):
        """Превратить смены уровня в длительности HIGH-импульсов (в микросекундах)."""
        durations = []
        for i in range(1, len(transitions)):
            prev_level, prev_t = transitions[i - 1]
            level, t = transitions[i]
            # Интересует только время, которое линия провела в HIGH: именно оно
            # кодирует бит. Умножаем на 1e6, чтобы перевести секунды в микросекунды.
            if prev_level == 1:
                durations.append((t - prev_t) * 1e6)
        return durations

    def read(self) -> dict:
        if GPIO is None:
            raise SensorError(f"{self.name}: RPi.GPIO not available on this host")
        transitions = self._capture_transitions()
        durations = self._decode(transitions)
        # В начале ответа есть служебные импульсы, поэтому берём последние 40:
        # это и есть данные. Если импульсов меньше 40, датчик не ответил как надо.
        bit_durations = durations[-40:] if len(durations) >= 40 else durations
        if len(bit_durations) != 40:
            raise SensorError(
                f"{self.name}: expected 40 bit pulses, got {len(bit_durations)} "
                f"({len(transitions)} raw transitions) — check wiring"
            )

        # Длинный импульс это 1, короткий это 0.
        bits = [1 if d > _BIT_THRESHOLD_MICROS else 0 for d in bit_durations]
        # Склеиваем 40 бит в 5 байт. Сдвиг "byte << 1" освобождает место справа,
        # а "| bit" записывает в него очередной бит (старшие биты идут первыми).
        data = []
        for byte_idx in range(5):
            byte = 0
            for bit in bits[byte_idx * 8:(byte_idx + 1) * 8]:
                byte = (byte << 1) | bit
            data.append(byte)

        # Пятый байт это контрольная сумма: сумма первых четырёх байт по модулю 256
        # ("& 0xFF" оставляет только младшие 8 бит). Не сошлась, значит данные
        # исказились и им нельзя доверять.
        checksum_ok = (sum(data[0:4]) & 0xFF) == data[4]
        if not checksum_ok:
            raise SensorError(f"{self.name}: checksum mismatch {data}")

        # Байты 0-1: влажность (целая и десятые доли), байты 2-3: температура.
        # У DHT11 дробные части обычно нулевые, он выдаёт целые числа.
        humidity = data[0] + data[1] / 10
        temperature = data[2] + data[3] / 10
        return {
            "temperature_celsius": temperature,
            "humidity_percent": humidity,
        }
