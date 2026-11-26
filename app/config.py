from pathlib import Path

import yaml

# config.yaml лежит рядом с этим файлом. Path(__file__).parent это каталог
# самого модуля, поэтому путь не зависит от того, откуда запущено приложение.
DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """Прочитать config.yaml и дополнить недостающие настройки значениями по умолчанию."""
    with open(path) as f:
        # safe_load разбирает только обычные данные (словари, списки, числа,
        # строки) и не выполняет код из файла, в отличие от небезопасного load.
        cfg = yaml.safe_load(f)

    # setdefault записывает значение, только если ключа ещё нет. Поэтому конфиг
    # можно сокращать: всё, что не указано, возьмётся отсюда.
    # 0.0.0.0 значит "слушать на всех сетевых интерфейсах", иначе VictoriaMetrics
    # из контейнера не смогла бы достучаться до приложения на хосте.
    cfg.setdefault("server", {}).setdefault("host", "0.0.0.0")
    cfg["server"].setdefault("port", 7777)
    cfg.setdefault("polling", {}).setdefault("interval_seconds", 5)
    # Эта настройка сейчас нигде не используется, оставлена на будущее.
    cfg["polling"].setdefault("metrics_update_seconds", cfg["polling"]["interval_seconds"])
    cfg.setdefault("collection", {}).setdefault("always_on", True)
    # Длительность замера, который запускает кнопка (или POST /collection/start).
    cfg["collection"].setdefault("duration_seconds", 180)
    if not cfg["collection"]["duration_seconds"] > 0:
        raise ValueError("collection.duration_seconds must be positive")
    # Сервопривод по умолчанию выключен: включать, когда он физически подключён.
    cfg.setdefault("servo", {}).setdefault("enabled", False)
    cfg.setdefault("button", {}).setdefault("enabled", False)
    snap = cfg.setdefault("thermal_snapshots", {})
    snap.setdefault("enabled", True)
    snap.setdefault("dir", "/opt/spa/data/thermal")
    snap.setdefault("keep", 30)  # число замеров, снимки которых хранятся
    rep = cfg.setdefault("reports", {})
    rep.setdefault("victoria_url", "http://localhost:8428")
    rep.setdefault("lookback_days", 31)
    # Пусто: ссылка на Grafana строится от адреса, по которому открыли /reports (порт 3000).
    rep.setdefault("grafana_url", "")
    rep.setdefault("norms", {})
    # Поиск теплопотерь: зона холоднее воздуха комнаты на delta_celsius и больше (СП 50.13330.2012,
    # табл. 5: 4,0 °C для наружных стен жилых зданий) или холоднее точки росы.
    hl = rep.setdefault("heat_loss", {})
    hl.setdefault("delta_celsius", 4.0)
    hl.setdefault("min_zone_pixels", 2)
    rep.setdefault("season", "auto")
    if rep["season"] not in ("auto", "cold", "warm"):
        raise ValueError("reports.season must be auto, cold or warm")
    cfg.setdefault("sensors", [])
    cfg.setdefault("logging", {}).setdefault("level", "INFO")
    return cfg
