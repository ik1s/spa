"""Физическая кнопка на GPIO: по нажатию вызывает on_press (запуск замера, см. main.py).

Кнопка одним выводом на GPIO, другим на GND. Внутренняя подтяжка к питанию (pull-up)
держит линию в HIGH, нажатие замыкает её на землю: событие "спад" (FALLING).
Дребезг контактов (десятки миллисекунд ложных срабатываний) гасит bouncetime.

На компьютере без Raspberry Pi модуля RPi.GPIO нет: тогда start() сообщает ButtonError,
а приложение продолжает работать без кнопки.
"""
import logging

try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

log = logging.getLogger("spa")


class ButtonError(Exception):
    """Кнопку не удалось настроить."""


class Button:
    def __init__(self, cfg: dict, on_press):
        self.pin = cfg["pin"]
        self.bounce_ms = int(cfg.get("bounce_ms", 300))
        self.on_press = on_press
        self._started = False

    def start(self):
        if GPIO is None:
            raise ButtonError("RPi.GPIO not available on this host")
        try:
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            # Без нажатия линия должна быть HIGH. LOW сразу после настройки значит, что
            # кнопка залипла или провод замкнут на землю: нажатия тогда не будут замечены.
            if GPIO.input(self.pin) == GPIO.LOW:
                log.warning("button GPIO%s reads LOW at start: check wiring (stuck or shorted to GND)", self.pin)
            GPIO.add_event_detect(self.pin, GPIO.FALLING, callback=self._callback, bouncetime=self.bounce_ms)
        except (RuntimeError, ValueError) as exc:
            raise ButtonError(f"cannot set up GPIO{self.pin}: {exc}") from exc
        self._started = True

    def _callback(self, channel):
        # Вызывается потоком RPi.GPIO: исключение здесь не должно убить обработку нажатий.
        log.info("button GPIO%s pressed", self.pin)
        try:
            self.on_press()
        except Exception as exc:
            log.info("button press ignored: %s", exc)

    def close(self):
        if self._started and GPIO is not None:
            GPIO.remove_event_detect(self.pin)
            self._started = False
