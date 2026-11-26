"""Страницы /reports: замеры и статистика по ним из VictoriaMetrics.

Приложение ничего не хранит само. Пока идёт замер, оно отдаёт метрику
spa_collection_session_start_timestamp_seconds{session="<ГГГГММДД_ЧЧММСС>"} (значение
= время начала). VictoriaMetrics снимает её раз в scrape_interval (10 с), поэтому:
- список замеров = серии этой метрики (export API за последние lookback_days);
- конец замера = время последнего отсчёта серии (точность около 10 с);
- min/среднее/max по датчику = min/avg/max_over_time за интервал замера.

Страницы:
- /reports         список замеров (новые сверху) со ссылками на подстраницы;
- /reports/<id>    таблица по замеру, ссылка на дашборд Grafana за время замера,
                   снимки тепловизора, сделанные во время замера.
"""
import html
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime

log = logging.getLogger("spa")

SESSION_METRIC = "spa_collection_session_start_timestamp_seconds"
# id замера = время начала ГГГГММДД_ЧЧММСС, оно же метка session.
ID_RE = re.compile(r"^\d{8}_\d{6}$")

# Строки таблицы: ключ норматива -> (название, единица, запросы min/среднее/max).
# {w} заменяется окном замера, например [190s].
ROWS = [
    ("temperature_celsius", "Температура воздуха", "°C",
     ("min_over_time(spa_temperature_celsius{w})", "avg_over_time(spa_temperature_celsius{w})",
      "max_over_time(spa_temperature_celsius{w})")),
    ("humidity_percent", "Относительная влажность", "%",
     ("min_over_time(spa_humidity_percent{w})", "avg_over_time(spa_humidity_percent{w})",
      "max_over_time(spa_humidity_percent{w})")),
    ("illuminance_lux", "Освещённость", "лк",
     ("min_over_time(spa_illuminance_lux{w})", "avg_over_time(spa_illuminance_lux{w})",
      "max_over_time(spa_illuminance_lux{w})")),
    # Тепловизор: самый холодный пиксель за замер, среднее по кадрам, самый горячий пиксель.
    ("thermal", "Температура поверхностей (тепловизор)", "°C",
     ("min_over_time(spa_thermal_min_celsius{w})", "avg_over_time(spa_thermal_mean_celsius{w})",
      "max_over_time(spa_thermal_max_celsius{w})")),
]


class VictoriaError(Exception):
    """VictoriaMetrics недоступна или ответила ошибкой."""


class Reports:
    def __init__(self, cfg: dict):
        self.vm_url = cfg["victoria_url"].rstrip("/")
        self.lookback = cfg["lookback_days"] * 86400
        self.norms = cfg["norms"]
        self.grafana_url = cfg["grafana_url"]

    # --- запросы к VictoriaMetrics ---

    def _get(self, path: str, params: dict) -> bytes:
        url = f"{self.vm_url}{path}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return resp.read()
        except OSError as exc:
            raise VictoriaError(f"{self.vm_url}: {exc}") from exc

    def _query(self, expr: str, at: float) -> dict:
        """Мгновенный запрос: {значение метки sensor: число}."""
        data = json.loads(self._get("/api/v1/query", {"query": expr, "time": f"{at:.3f}"}))
        if data.get("status") != "success":
            raise VictoriaError(f"query failed: {data.get('error')}")
        return {r["metric"].get("sensor", ""): float(r["value"][1]) for r in data["data"]["result"]}

    def sessions(self) -> list:
        """Замеры за lookback_days, новые сверху: [{"id", "start", "end"}]."""
        now = time.time()
        raw = self._get("/api/v1/export", {
            "match[]": SESSION_METRIC, "start": int(now - self.lookback), "end": int(now),
        })
        found = {}
        # export отдаёт по строке JSON на серию: {"metric": {...}, "values": [...], "timestamps": [...]}.
        for line in raw.splitlines():
            if not line.strip():
                continue
            series = json.loads(line)
            sid = series["metric"].get("session", "")
            if not ID_RE.match(sid) or not series["timestamps"]:
                continue
            s = found.setdefault(sid, {"id": sid, "start": series["values"][0], "end": 0.0})
            s["end"] = max(s["end"], max(series["timestamps"]) / 1000)
        return sorted(found.values(), key=lambda s: s["start"], reverse=True)

    def session(self, sid: str):
        if not ID_RE.match(sid):
            return None
        return next((s for s in self.sessions() if s["id"] == sid), None)

    def stats(self, session: dict) -> list:
        """Строки таблицы: (ключ, название, единица, датчик, min, среднее, max)."""
        # Окно от начала замера до последнего отсчёта плюс один интервал скрейпа с запасом.
        at = session["end"] + 5
        window = f"[{int(at - session['start']) + 15}s]"
        rows = []
        for key, title, unit, exprs in ROWS:
            lo, mean, hi = (self._query(e.replace("{w}", window), at) for e in exprs)
            for sensor in sorted(mean):
                rows.append((key, title, unit, sensor, lo.get(sensor), mean[sensor], hi.get(sensor)))
        return rows

    # --- HTML ---

    def render_index(self, current_id) -> bytes:
        try:
            sessions = self.sessions()
        except (VictoriaError, ValueError) as exc:
            return _page("Замеры", f"<h1>Замеры</h1><p>Не удалось получить замеры из VictoriaMetrics: "
                                    f"{html.escape(str(exc))}</p>")
        if not sessions:
            body = ("<p>Замеров за последние дни нет. Нажмите кнопку на устройстве или отправьте "
                    "POST /collection/start.</p>")
        else:
            items = "".join(
                f"<tr><td><a href=\"/reports/{s['id']}\">{_fmt_time(s['start'])}</a></td>"
                f"<td>{_duration(s, s['id'] == current_id)}</td>"
                f"<td>{'идёт' if s['id'] == current_id else 'завершён'}</td></tr>"
                for s in sessions
            )
            body = f"<table><tr><th>Начало</th><th>Длительность</th><th>Статус</th></tr>{items}</table>"
        return _page("Замеры", f"<h1>Замеры</h1>{body}")

    def render_report(self, sid: str, current_id, snapshots_between, request_host: str):
        """HTML страницы замера или None, если такого замера нет."""
        try:
            session = self.session(sid)
            if session is None:
                return None
            rows = self.stats(session)
        except (VictoriaError, ValueError) as exc:
            return _page("Замер", f"<p><a href=\"/reports\">← все замеры</a></p><p>Не удалось получить "
                                   f"данные из VictoriaMetrics: {html.escape(str(exc))}</p>")
        running = sid == current_id
        end = time.time() if running else session["end"]
        snapshots = snapshots_between(session["start"], end + 10)
        table_rows = "".join(
            f"<tr><td>{html.escape(title)}, {unit}</td><td>{html.escape(sensor)}</td>"
            f"<td class=\"num\">{_num(lo)}</td><td class=\"num\">{_num(mean)}</td><td class=\"num\">{_num(hi)}</td>"
            f"<td>{html.escape(self.norms.get(key, '—'))}</td></tr>"
            for key, title, unit, sensor, lo, mean, hi in rows
        )
        table = (
            "<table><tr><th>Показатель</th><th>Датчик</th><th>Минимум</th><th>Среднее</th><th>Максимум</th>"
            f"<th>Норматив СанПиН</th></tr>{table_rows}</table>"
            if rows else "<p>За этот замер в VictoriaMetrics нет показаний датчиков.</p>"
        )
        grafana = self.grafana_url or f"http://{request_host}:3000"
        # Запас 10 с по краям, чтобы на графиках были видны крайние точки замера.
        dashboard = (f"{grafana.rstrip('/')}/d/spa-sensors/?from={int(session['start'] * 1000) - 10_000}"
                     f"&to={int(end * 1000) + 10_000}")
        snaps = "".join(
            f"<a href=\"/snapshots/{html.escape(n)}\"><img src=\"/snapshots/{html.escape(n)}\" alt=\"{html.escape(n)}\"></a>"
            for n in snapshots
        )
        body = (
            "<p><a href=\"/reports\">← все замеры</a></p>"
            f"<h1>Замер {_fmt_time(session['start'])}</h1>"
            f"<p>{'Идёт' if running else 'Окончание: ' + _fmt_time(session['end'])}, "
            f"длительность {_duration({'start': session['start'], 'end': end}, False)}.</p>"
            f"<p><a href=\"{html.escape(dashboard)}\">Графики за время замера в Grafana</a></p>"
            f"{table}"
            "<p class=\"note\">Значения из VictoriaMetrics (отсчёты раз в 10 с), время окончания замера "
            "с точностью до 10 с. Нормативы для жилой комнаты по СанПиН 1.2.3685-21; микроклимат "
            "нормируется в обслуживаемой зоне (0,1–2,0 м от пола, не ближе 0,5 м от стен, окон и "
            "отопления). Результаты бытовых датчиков не заменяют замеров аккредитованной лаборатории.</p>"
            + (f"<h2>Снимки тепловизора</h2>{snaps}"
               "<p class=\"note\">Цвета: тёмно-фиолетовый — самый холодный пиксель кадра, "
               "светло-жёлтый — самый горячий; шкала своя у каждого снимка.</p>" if snaps else "")
        )
        return _page(f"Замер {sid}", body)


_STYLE = """
body{font-family:sans-serif;margin:2em;max-width:62em;color:#222}
table{border-collapse:collapse;margin:1em 0}
th,td{border:1px solid #bbb;padding:.35em .7em;text-align:left}
th{background:#eee} td.num{text-align:right;font-variant-numeric:tabular-nums}
.note{color:#666;font-size:.9em} img{image-rendering:pixelated;margin:.3em;border:1px solid #bbb}
"""


def _page(title: str, body: str) -> bytes:
    return (
        "<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    ).encode()


def _fmt_time(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M:%S")


def _duration(s: dict, running: bool) -> str:
    end = time.time() if running else s["end"]
    seconds = max(0, int(end - s["start"]))
    return f"{seconds // 60} мин {seconds % 60:02d} с"


def _num(value) -> str:
    return "—" if value is None else f"{value:.1f}".replace(".", ",")


def session_id(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y%m%d_%H%M%S")


def snapshot_time(name: str):
    """Время снимка из имени thermal_ГГГГММДД_ЧЧММСС.png (или None)."""
    try:
        return datetime.strptime(name[len("thermal_"):-len(".png")], "%Y%m%d_%H%M%S").timestamp()
    except ValueError:
        return None
