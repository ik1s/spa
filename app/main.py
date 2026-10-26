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
from sensors.base import SensorError

log = logging.getLogger("spa")


class Collector:
    """Опрашивает все датчики из конфига в фоновом потоке."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.sensors = [create_sensor(s) for s in cfg["sensors"]]
        self.health = HealthState([s.name for s in self.sensors], cfg["collection"]["always_on"])
        self.collection_enabled = cfg["collection"]["always_on"]
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

    def _poll_one(self, sensor):
        """Опросить один датчик и обновить метрики и состояние для /health."""
        # Время попытки пишем до чтения: так видно, что опрос идёт, даже если
        # датчик отвечает с ошибкой.
        metrics.sensor_last_read_timestamp_seconds.labels(sensor=sensor.name).set(time.time())
        try:
            values = sensor.read()
        except SensorError as exc:
            # Ошибка датчика не должна ронять приложение: фиксируем и идём дальше.
            log.warning("sensor %s read failed: %s", sensor.name, exc)
            metrics.sensor_up.labels(sensor=sensor.name).set(0)
            metrics.sensor_read_errors_total.labels(sensor=sensor.name).inc()
            self.health.record_error(sensor.name, str(exc))
            return

        metrics.sensor_up.labels(sensor=sensor.name).set(1)
        if "temperature_celsius" in values:
            metrics.temperature_celsius.labels(sensor=sensor.name).set(values["temperature_celsius"])
        if "humidity_percent" in values:
            metrics.humidity_percent.labels(sensor=sensor.name).set(values["humidity_percent"])
        self.health.record_success(sensor.name, values)


def make_handler(collector: Collector):
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

    # ThreadingHTTPServer обрабатывает каждый запрос в отдельном потоке.
    server = ThreadingHTTPServer(
        (cfg["server"]["host"], cfg["server"]["port"]), make_handler(collector)
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
        log.info("stopped")


if __name__ == "__main__":
    sys.exit(main())
