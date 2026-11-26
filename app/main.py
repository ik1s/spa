"""Точка входа: HTTP-сервер и фоновый опрос датчиков.

Приложение работает в двух потоках одновременно:
- главный поток обслуживает HTTP-запросы (/metrics, /health, /collection, /servo, /thermal, /reports);
- отдельный поток (Collector) раз в несколько секунд опрашивает датчики и
  обновляет метрики. Опрос идёт всегда (collection.always_on: true) или только во
  время замера: замер на collection.duration_seconds запускает кнопка или
  POST /collection/start.
Так медленное чтение датчика не задерживает ответы на запросы.
"""
import json
import logging
import re
import signal
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import metrics
from config import load_config
from health import HealthState
from sensors import create_sensor
from button import Button, ButtonError
from servo import Servo, ServoBusy, ServoError
from sensors.base import SensorError
from thermal_image import PngCache, SnapshotStore
import reports

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
        self.always_on = cfg["collection"]["always_on"]
        # Длительность замера, который запускает кнопка или POST /collection/start.
        self.duration = cfg["collection"]["duration_seconds"]
        self.health = HealthState([s.name for s in self.sensors], self.always_on)
        # Время окончания текущего замера (по time.monotonic()) или None, если замера нет.
        # Его меняют поток кнопки и HTTP-потоки, а читает поток опроса, поэтому под замком.
        self._lock = threading.Lock()
        self._session_until = None
        # Был ли сбор включён на прошлом шаге цикла: по смене состояния пишем лог и метрики.
        self._was_active = None
        # Снимки тепловизора: первый удачный кадр после каждого запуска замера сохраняется в папку.
        snap = cfg["thermal_snapshots"]
        self.snapshots = SnapshotStore(snap["dir"], snap["keep"]) if snap["enabled"] else None
        self._snapshot_pending = False
        # id текущего замера (время начала ГГГГММДД_ЧЧММСС) для метки session, None вне замера.
        self.session_id = None
        # Сколько опросов подряд датчик отвечает ошибкой и когда его переподключали в последний раз.
        self._failures = {s.name: 0 for s in self.sensors}
        self._last_reconnect = {s.name: 0.0 for s in self.sensors}
        # Event это флаг "пора остановиться", безопасный для нескольких потоков.
        self._stop = threading.Event()
        # Будит поток опроса раньше конца паузы: замер стартует сразу после нажатия.
        self._wake = threading.Event()
        # daemon=True: если главный поток завершился, этот поток не будет
        # удерживать процесс и умрёт вместе с ним.
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        # Ждём завершения потока не дольше 5 секунд, чтобы не зависнуть навсегда.
        self._thread.join(timeout=5)

    def _remaining(self) -> float:
        """Сколько секунд осталось до конца замера (0, если замера нет). Вызывать под замком."""
        if self._session_until is None:
            return 0.0
        return max(0.0, self._session_until - time.monotonic())

    def is_collecting(self) -> bool:
        if self.always_on:
            return True
        with self._lock:
            return self._remaining() > 0

    def start_session(self) -> dict:
        """Начать замер на duration секунд. Повторный вызов во время замера отсчитывает время заново."""
        with self._lock:
            restarted = self._remaining() > 0
            self._session_until = time.monotonic() + self.duration
            self._snapshot_pending = True
            # Повторное нажатие во время замера продлевает его: это тот же замер в /reports.
            if not restarted and not self.always_on:
                started = time.time()
                self.session_id = reports.session_id(started)
                # Прошлый замер мог истечь, а поток опроса ещё не успел убрать его серию.
                metrics.clear_sessions()
                metrics.collection_session_start.labels(session=self.session_id).set(int(started))
        log.info("measurement %s for %g s", "restarted" if restarted else "started", self.duration)
        self._wake.set()
        return self.collection_status()

    def stop_session(self) -> dict:
        with self._lock:
            was_running = self._remaining() > 0
            self._session_until = None
        if was_running:
            log.info("measurement stopped by request")
        self._wake.set()
        return self.collection_status()

    def collection_status(self) -> dict:
        with self._lock:
            remaining = self._remaining()
        return {
            "always_on": self.always_on,
            "collecting": self.always_on or remaining > 0,
            "remaining_seconds": round(remaining, 1),
            "duration_seconds": self.duration,
        }

    def _apply_state(self, active: bool):
        """Обновить метрики, /health и лог при включении и выключении сбора."""
        with self._lock:
            remaining = self._remaining()
        metrics.collection_remaining_seconds.set(round(remaining, 1))
        if active == self._was_active:
            return
        metrics.collection_enabled.set(1 if active else 0)
        self.health.set_collection_enabled(active)
        if self._was_active and not active:
            # Замер закончился: убираем показания из /metrics, чтобы в Grafana был
            # разрыв до следующего замера, а не линия из последних значений.
            for sensor in self.sensors:
                metrics.clear_readings(sensor.name)
            metrics.clear_sessions()
            with self._lock:
                self.session_id = None
            log.info("measurement finished")
        self._was_active = active

    def _run(self):
        interval = self.cfg["polling"]["interval_seconds"]
        while not self._stop.is_set():
            self._wake.clear()
            active = self.is_collecting()
            self._apply_state(active)
            if active:
                for sensor in self.sensors:
                    if self._stop.is_set():
                        break
                    self._poll_one(sensor)
            # Ждём до следующего опроса, но не дольше конца замера, чтобы он закончился
            # вовремя. wait() просыпается сразу при stop() или нажатии кнопки.
            timeout = interval
            if not self.always_on:
                with self._lock:
                    remaining = self._remaining()
                if remaining > 0:
                    timeout = min(interval, remaining)
            self._wake.wait(timeout)

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
        if self._snapshot_pending and self.snapshots is not None and hasattr(sensor, "last_frame"):
            self._snapshot_pending = False
            path = self.snapshots.save(sensor.last_frame())
            if path is not None:
                log.info("thermal snapshot saved: %s", path)


# Имя снимка тепловизора: только такие файлы отдаёт /snapshots/ (защита от ../ в пути).
SNAPSHOT_RE = re.compile(r"^thermal_\d{8}_\d{6}\.png$")


def make_handler(collector: Collector, servo=None):
    png_cache = PngCache()
    report_pages = reports.Reports(collector.cfg["reports"])

    # Класс обработчика объявлен внутри функции, чтобы его методы видели
    # переменную collector: сервер сам создаёт обработчик на каждый запрос и
    # передать ему свои данные другим способом неудобно.
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Вместо стандартной печати в stderr пишем в общий лог приложения.
            log.info("%s - %s", self.address_string(), fmt % args)

        def _send_bytes(self, status: int, content_type: str, body: bytes):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, status: int, body: bytes):
            self._send_bytes(status, "text/html; charset=utf-8", body)

        def _request_host(self) -> str:
            """Хост, по которому открыли страницу (без порта): Grafana на нём же, порт 3000."""
            host = urllib.parse.urlsplit("//" + (self.headers.get("Host") or "localhost")).hostname or "localhost"
            return f"[{host}]" if ":" in host else host

        def _snapshots_between(self, start: float, end: float) -> list:
            """Имена снимков тепловизора, сделанных с start по end (время берётся из имени файла)."""
            snaps = collector.snapshots
            if snaps is None or not snaps.dir.is_dir():
                return []
            names = sorted(p.name for p in snaps.dir.glob("thermal_*.png"))
            # Снимок сохраняется после первого опроса, через секунды после начала замера:
            # секунда запаса в начале на округление времени в имени файла.
            return [n for n in names if (t := reports.snapshot_time(n)) is not None and start - 1 <= t <= end]

        def _send_json(self, status: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.path = self.path.split("?", 1)[0]
            if self.path in ("/collection/start", "/collection/stop"):
                if collector.always_on:
                    # Сбор и так идёт всегда: запускать и останавливать нечего.
                    self._send_json(409, {"status": "always_on", **collector.collection_status()})
                elif self.path == "/collection/start":
                    self._send_json(200, collector.start_session())
                else:
                    self._send_json(200, collector.stop_session())
                return
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
            # Параметры запроса (например ?t=... от Grafana против кэша браузера) не важны.
            self.path = self.path.split("?", 1)[0]
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
            elif self.path in ("/thermal", "/thermal.png"):
                # Последний кадр тепловизора (сохраняется и после конца замера, смотрите timestamp).
                frames = [s.last_frame() for s in collector.sensors if hasattr(s, "last_frame")]
                if not frames:
                    self._send_json(404, {"status": "no_thermal_sensor"})
                elif frames[0] is None:
                    self._send_json(503, {"status": "no_frame_yet"})
                elif self.path == "/thermal":
                    self._send_json(200, frames[0])
                else:
                    body = png_cache.get(frames[0])
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(body)))
                    # Картинка меняется с каждым кадром: браузер не должен показывать старую из кэша.
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
            elif self.path == "/collection":
                self._send_json(200, collector.collection_status())
            elif self.path == "/servo":
                if servo is None:
                    self._send_json(503, {"status": "servo_disabled"})
                else:
                    self._send_json(200, servo.status())
            elif self.path in ("/reports", "/reports/"):
                self._send_html(200, report_pages.render_index(collector.session_id))
            elif self.path.startswith("/reports/"):
                sid = self.path[len("/reports/"):]
                page = report_pages.render_report(sid, collector.session_id, self._snapshots_between, self._request_host())
                if page is None:
                    self._send_json(404, {"status": "not_found"})
                else:
                    self._send_html(200, page)
            elif self.path.startswith("/snapshots/"):
                name = self.path[len("/snapshots/"):]
                snaps = collector.snapshots
                path = snaps.dir / name if snaps and SNAPSHOT_RE.match(name) else None
                if path is None or not path.is_file():
                    self._send_json(404, {"status": "not_found"})
                else:
                    self._send_bytes(200, "image/png", path.read_bytes())
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

    def on_button_press():
        # Кнопка запускает замер; если подключена серва, она заодно делает проход.
        collector.start_session()
        if servo is not None:
            try:
                servo.sweep()
            except ServoBusy:
                log.info("servo sweep already running")

    button = None
    if cfg["button"]["enabled"]:
        if collector.always_on:
            log.warning("button is enabled but collection.always_on is true: press only starts a servo sweep")
        button = Button(cfg["button"], on_button_press)
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
