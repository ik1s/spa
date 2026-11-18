"""Точка входа: HTTP-сервер и фоновый опрос датчиков.

Приложение работает в двух потоках одновременно:
- главный поток обслуживает HTTP-запросы (/metrics, /health, /reports);
- отдельный поток (Collector) раз в несколько секунд опрашивает датчики и
  обновляет метрики.
Так медленное чтение датчика не задерживает ответы на запросы.
"""
import json
import logging
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import metrics
from config import load_config
from health import HealthState
from sensors import create_sensor
from button import Button, ButtonError
from servo import Servo, ServoBusy, ServoError
from sensors.base import SensorError

log = logging.getLogger("spa")

# Сколько раз пробуем прочитать датчик за один опрос и пауза между попытками.
# DHT11 по даташиту отвечает не чаще раза в секунду, поэтому пауза 1 с.
_READ_ATTEMPTS = 3
_RETRY_PAUSE_SECONDS = 1.1

# Переподключение датчика: после стольких неудачных опросов подряд (каждый уже
# включает _READ_ATTEMPTS попыток) и не чаще, чем раз в _RECONNECT_COOLDOWN_SECONDS.
_RECONNECT_AFTER_FAILURES = 3
_RECONNECT_COOLDOWN_SECONDS = 60


class Collector:
    """Опрашивает все датчики из конфига в фоновом потоке."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.sensors = [create_sensor(s) for s in cfg["sensors"]]
        self.health = HealthState([s.name for s in self.sensors], cfg["collection"]["always_on"])
        self.collection_enabled = cfg["collection"]["always_on"]
        # Сколько опросов подряд датчик отвечает ошибкой и когда его переподключали в последний раз.
        self._failures = {s.name: 0 for s in self.sensors}
        self._last_reconnect = {s.name: 0.0 for s in self.sensors}
        # Event это флаг "пора остановиться", безопасный для нескольких потоков.
        self._stop = threading.Event()
        # daemon=True: если главный поток завершился, этот поток не будет
        # удерживать процесс и умрёт вместе с ним.
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        # Ждём завершения потока не дольше 5 секунд, чтобы не зависнуть навсегда.
        self._thread.join(timeout=5)

    def _run(self):
        interval = self.cfg["polling"]["interval_seconds"]
        metrics.collection_enabled.set(1 if self.collection_enabled else 0)
        while not self._stop.is_set():
            if self.collection_enabled:
                for sensor in self.sensors:
                    self._poll_one(sensor)
            # wait() работает как sleep(interval), но просыпается сразу, как
            # только вызван stop(). Обычный sleep заставил бы ждать до конца паузы.
            self._stop.wait(interval)

    def _read_with_retries(self, sensor):
        """Прочитать датчик, при сбое повторить чтение (одиночные сбои DHT11 обычное дело)."""
        for attempt in range(1, _READ_ATTEMPTS + 1):
            try:
                return sensor.read()
            except SensorError as exc:
                if attempt == _READ_ATTEMPTS:
                    raise
                # Одиночный сбой пишем на уровне DEBUG, чтобы не засорять журнал.
                log.debug("sensor %s attempt %d failed: %s", sensor.name, attempt, exc)
                # DHT11 нельзя опрашивать чаще раза в секунду, но короткая пауза
                # перед повтором на практике помогает; stop() прерывает ожидание.
                if self._stop.wait(_RETRY_PAUSE_SECONDS):
                    raise

    def _maybe_reconnect(self, sensor):
        """Если датчик долго не читается, программно переподключить его (с паузой между попытками)."""
        if self._failures[sensor.name] < _RECONNECT_AFTER_FAILURES:
            return
        now = time.monotonic()
        if now - self._last_reconnect[sensor.name] < _RECONNECT_COOLDOWN_SECONDS:
            return
        self._last_reconnect[sensor.name] = now
        metrics.sensor_reconnects_total.labels(sensor=sensor.name).inc()
        log.warning("sensor %s failed %d polls in a row, reconnecting", sensor.name, self._failures[sensor.name])
        try:
            sensor.reconnect()
        except SensorError as exc:
            log.warning("sensor %s reconnect error: %s", sensor.name, exc)

    def _poll_one(self, sensor):
        """Опросить один датчик и обновить метрики и состояние для /health."""
        # Время попытки пишем до чтения: так видно, что опрос идёт, даже если
        # датчик отвечает с ошибкой.
        metrics.sensor_last_read_timestamp_seconds.labels(sensor=sensor.name).set(time.time())
        try:
            values = self._read_with_retries(sensor)
        except SensorError as exc:
            # Ошибка датчика не должна ронять приложение: фиксируем и идём дальше.
            log.warning("sensor %s read failed after %d attempts: %s", sensor.name, _READ_ATTEMPTS, exc)
            metrics.sensor_up.labels(sensor=sensor.name).set(0)
            metrics.sensor_read_errors_total.labels(sensor=sensor.name).inc()
            # Старые значения из /metrics убираем, чтобы они не выглядели свежими.
            metrics.clear_readings(sensor.name)
            self.health.record_error(sensor.name, str(exc))
            self._failures[sensor.name] += 1
            self._maybe_reconnect(sensor)
            return

        self._failures[sensor.name] = 0
        metrics.sensor_up.labels(sensor=sensor.name).set(1)
        for key, gauge in metrics.READINGS.items():
            if key in values:
                gauge.labels(sensor=sensor.name).set(values[key])
        self.health.record_success(sensor.name, values)


def make_handler(collector: Collector, servo=None):
    # Класс обработчика объявлен внутри функции, чтобы его методы видели
    # переменную collector: сервер сам создаёт обработчик на каждый запрос и
    # передать ему свои данные другим способом неудобно.
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Вместо стандартной печати в stderr пишем в общий лог приложения.
            log.info("%s - %s", self.address_string(), fmt % args)

        def _send_json(self, status: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path not in ("/servo", "/servo/sweep"):
                self._send_json(404, {"status": "not_found"})
                return
            if servo is None:
                self._send_json(503, {"status": "servo_disabled"})
                return
            if self.path == "/servo/sweep":
                try:
                    servo.sweep()
                except ServoBusy as exc:
                    self._send_json(409, {"status": "busy", "error": str(exc)})
                else:
                    self._send_json(202, servo.status())
                return
            # Тело запроса маленькое ({"angle": 90}): большие не читаем, чтобы нельзя было забить память.
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 256:
                self._send_json(400, {"status": "bad_request", "error": "body must be 1-256 bytes"})
                return
            try:
                angle = json.loads(self.rfile.read(length))["angle"]
                servo.set_angle(angle)
            except (ValueError, KeyError, TypeError):
                self._send_json(400, {"status": "bad_request", "error": 'expected JSON {"angle": <number>}'})
            except ServoError as exc:
                self._send_json(400, {"status": "bad_request", "error": str(exc)})
            else:
                self._send_json(200, servo.status())

        def do_GET(self):
            if self.path == "/metrics":
                body = metrics.render()
                self.send_response(200)
                # Именно такой Content-Type ожидает Prometheus-совместимый сборщик.
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/health":
                self._send_json(200, collector.health.to_dict())
            elif self.path == "/servo":
                if servo is None:
                    self._send_json(503, {"status": "servo_disabled"})
                else:
                    self._send_json(200, servo.status())
            elif self.path == "/reports":
                # 501 Not Implemented: отчёты ещё не сделаны.
                self._send_json(501, {"status": "not_implemented"})
            else:
                self._send_json(404, {"status": "not_found"})

    return Handler


def main():
    cfg = load_config()
    logging.basicConfig(
        # Уровень из конфига ("INFO", "DEBUG"...). Если написано что-то
        # неизвестное, используем INFO.
        level=getattr(logging, cfg["logging"]["level"].upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    collector = Collector(cfg)
    collector.start()

    servo = None
    if cfg["servo"]["enabled"]:
        servo = Servo(cfg["servo"])
        servo.on_move = metrics.servo_angle_degrees.set

    button = None
    if cfg["button"]["enabled"]:
        if servo is None:
            log.warning("button is enabled but servo is disabled: button ignored")
        else:
            button = Button(cfg["button"], servo.sweep)
            try:
                button.start()
            except ButtonError as exc:
                log.warning("button disabled: %s", exc)
                button = None

    # ThreadingHTTPServer обрабатывает каждый запрос в отдельном потоке.
    server = ThreadingHTTPServer(
        (cfg["server"]["host"], cfg["server"]["port"]), make_handler(collector, servo)
    )

    def shutdown(signum, frame):
        log.info("received signal %s, shutting down", signum)
        # server.shutdown() нельзя вызывать прямо из обработчика сигнала: он
        # ждёт выхода из serve_forever(), а тот выполняется в этом же потоке,
        # и получится взаимная блокировка (дедлок). Поэтому запускаем в отдельном потоке.
        threading.Thread(target=server.shutdown, daemon=True).start()

    # SIGTERM присылает systemd при "systemctl stop", SIGINT это Ctrl+C.
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info("spa listening on %s:%s", cfg["server"]["host"], cfg["server"]["port"])
    try:
        server.serve_forever()
    finally:
        # Что бы ни случилось, останавливаем поток опроса перед выходом.
        collector.stop()
        if button is not None:
            button.close()
        if servo is not None:
            servo.close()
        log.info("stopped")


if __name__ == "__main__":
    sys.exit(main())
