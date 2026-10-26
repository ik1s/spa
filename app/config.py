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
    cfg.setdefault("sensors", [])
    cfg.setdefault("logging", {}).setdefault("level", "INFO")
    return cfg
