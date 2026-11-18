"""Сервопривод SG90 на аппаратном ШИМ Raspberry Pi 5.

Серва управляется импульсом раз в 20 мс: ширина импульса задаёт угол. Для SG90
это примерно 0,5 мс (0 градусов) ... 2,4 мс (180 градусов); границы можно уточнить
в config.yaml, у разных экземпляров они чуть отличаются.

ШИМ даёт ядро Linux: после включения оверлея pwm в /boot/firmware/config.txt
появляется каталог /sys/class/pwm/pwmchip0, и мы просто пишем числа в его файлы.
Аппаратный ШИМ не дрожит, в отличие от программного.

Угол меняется плавно (speed_deg_per_s), в отдельном потоке: резкие рывки сервы
дают скачки тока, а плавное движение ещё и бережёт механику платформы.
"""
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("spa")

_PERIOD_NS = 20_000_000  # 20 мс, стандартный период сервоимпульса (50 Гц)
_STEP_SECONDS = 0.02     # как часто обновляем положение при движении


class ServoError(Exception):
    """Сервопривод недоступен или команда неверна."""


class ServoBusy(ServoError):
    """Серва уже выполняет проход: новый проход не запускаем."""


class Servo:
    def __init__(self, cfg: dict, sysfs_root: str = "/sys/class/pwm"):
        self.min_angle = float(cfg.get("min_angle", 0))
        self.max_angle = float(cfg.get("max_angle", 180))
        self.min_pulse_us = float(cfg.get("min_pulse_us", 500))
        self.max_pulse_us = float(cfg.get("max_pulse_us", 2400))
        self.speed = float(cfg.get("speed_deg_per_s", 60))
        if not (0 <= self.min_angle < self.max_angle <= 180):
            raise ValueError("servo angles must satisfy 0 <= min < max <= 180")
        if not (0 < self.min_pulse_us < self.max_pulse_us < _PERIOD_NS / 1000):
            raise ValueError("servo pulse widths are out of range")
        if self.speed <= 0:
            raise ValueError("servo speed_deg_per_s must be positive")

        chip = cfg.get("pwm_chip", 0)
        channel = cfg.get("pwm_channel", 0)
        self._chip_dir = Path(sysfs_root) / f"pwmchip{chip}"
        self._pwm_dir = self._chip_dir / f"pwm{channel}"
        self._channel = channel

        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        # None до первой команды: реальное положение сервы после включения неизвестно.
        self._current = None
        # Очередь целевых углов: серва идёт к первому, потом к следующему. Обычная команда
        # это очередь из одного угла, проход (sweep) это три угла: min, max, min.
        self._plan = []
        self._sweeping = False
        self._enabled = False
        self.on_move = None  # необязательный обратный вызов: on_move(угол), для метрик
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self._enabled,
                "angle": None if self._current is None else round(self._current, 1),
                "target": self._plan[-1] if self._plan else None,
                "sweeping": self._sweeping,
                "min_angle": self.min_angle,
                "max_angle": self.max_angle,
            }

    def set_angle(self, angle) -> float:
        """Задать целевой угол. Проверяет значение и сразу возвращается: движение идёт в потоке."""
        # bool это подкласс int, но True вместо угла почти наверняка ошибка клиента.
        if isinstance(angle, bool) or not isinstance(angle, (int, float)):
            raise ServoError("angle must be a number")
        if not (self.min_angle <= angle <= self.max_angle):
            raise ServoError(f"angle must be between {self.min_angle:g} and {self.max_angle:g}")
        with self._lock:
            # Обычная команда отменяет проход, если он шёл.
            self._plan = [float(angle)]
            self._sweeping = False
        self._wake.set()
        return float(angle)

    def sweep(self):
        """Пройти от min_angle до max_angle и обратно. Если проход уже идёт, ServoBusy."""
        with self._lock:
            if self._sweeping:
                raise ServoBusy("sweep already running")
            self._plan = [self.min_angle, self.max_angle, self.min_angle]
            self._sweeping = True
        self._wake.set()

    def close(self):
        """Остановить поток и выключить ШИМ: серва перестаёт держать положение и греться."""
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=2)
        self._disable()

    # --- внутреннее ---

    def _write(self, name: str, value):
        try:
            (self._pwm_dir / name).write_text(str(value))
        except OSError as exc:
            raise ServoError(f"cannot write {self._pwm_dir / name}: {exc}") from exc

    def _enable(self):
        if not self._pwm_dir.exists():
            if not self._chip_dir.exists():
                raise ServoError(
                    f"{self._chip_dir} not found: enable the pwm overlay in /boot/firmware/config.txt and reboot"
                )
            try:
                (self._chip_dir / "export").write_text(str(self._channel))
            except OSError as exc:
                raise ServoError(f"cannot export pwm channel: {exc}") from exc
            # udev создаёт файлы канала не мгновенно.
            for _ in range(20):
                if (self._pwm_dir / "enable").exists():
                    break
                time.sleep(0.05)
        # Сначала duty 0, потом период: duty не может быть больше периода.
        self._write("duty_cycle", 0)
        self._write("period", _PERIOD_NS)
        self._write("duty_cycle", self._duty_ns(self._current))
        self._write("enable", 1)
        self._enabled = True

    def _disable(self):
        if self._enabled:
            try:
                self._write("enable", 0)
            except ServoError as exc:
                log.warning("servo disable failed: %s", exc)
            self._enabled = False

    def _duty_ns(self, angle: float) -> int:
        span = self.max_angle - self.min_angle
        fraction = (angle - self.min_angle) / span
        pulse_us = self.min_pulse_us + fraction * (self.max_pulse_us - self.min_pulse_us)
        return int(pulse_us * 1000)

    def _run(self):
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            try:
                self._move()
            except ServoError as exc:
                log.warning("servo error: %s", exc)

    def _move(self):
        while not self._stop.is_set():
            with self._lock:
                if not self._plan:
                    self._sweeping = False
                    return
                target = self._plan[0]
                current = self._current
            if current == target:
                with self._lock:
                    # Очередь могла смениться командой из другого потока: убираем только
                    # тот угол, который действительно достигнут.
                    if self._plan and self._plan[0] == target:
                        self._plan.pop(0)
                continue
            if current is None:
                # Первое включение: положение неизвестно, прыгаем сразу в цель.
                nxt = target
                self._current = nxt
                self._enable()
            else:
                step = self.speed * _STEP_SECONDS
                nxt = target if abs(target - current) <= step else current + step * (1 if target > current else -1)
                with self._lock:
                    self._current = nxt
                self._write("duty_cycle", self._duty_ns(nxt))
            if self.on_move:
                self.on_move(nxt)
            if self._stop.wait(_STEP_SECONDS):
                return
