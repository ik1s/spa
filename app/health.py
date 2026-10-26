import threading
from datetime import datetime


class HealthState:
    """Состояние для эндпоинта /health: общий статус и статус каждого датчика.

    Данные пишет поток опроса датчиков, а читают HTTP-потоки, то есть к ним
    обращаются из разных потоков одновременно. Поэтому каждое обращение
    защищено замком (Lock): пока один поток работает с данными, остальные ждут.
    """

    def __init__(self, sensor_names, collection_enabled: bool):
        self._lock = threading.Lock()
        self._collection_enabled = collection_enabled
        # До первого опроса у каждого датчика статус "unknown".
        self._sensors = {
            name: {"status": "unknown", "last_read": None, "last_error": None, "values": {}}
            for name in sensor_names
        }

    def set_collection_enabled(self, enabled: bool):
        with self._lock:
            self._collection_enabled = enabled

    def record_success(self, name: str, values: dict):
        with self._lock:
            self._sensors[name] = {
                "status": "ok",
                # astimezone() добавляет к времени локальный часовой пояс,
                # isoformat() превращает его в строку вида 2026-10-03T16:19:01+03:00.
                "last_read": datetime.now().astimezone().isoformat(),
                "last_error": None,
                "values": values,
            }

    def record_error(self, name: str, error: str):
        with self._lock:
            prev = self._sensors.get(name, {})
            self._sensors[name] = {
                "status": "error",
                "last_read": datetime.now().astimezone().isoformat(),
                "last_error": error,
                # При ошибке сохраняем последние удачные значения, чтобы в
                # /health было видно, какими они были до сбоя.
                "values": prev.get("values", {}),
            }

    def to_dict(self) -> dict:
        # Под замком делаем копию и сразу отпускаем его: дальнейшие вычисления
        # не должны задерживать поток опроса.
        with self._lock:
            sensors = {k: dict(v) for k, v in self._sensors.items()}
            collection_enabled = self._collection_enabled

        # Общий статус по статусам датчиков:
        #   все ok -> "ok"; датчиков нет -> "unknown";
        #   часть ok, часть нет -> "degraded"; ни один не ok -> "error".
        statuses = {s["status"] for s in sensors.values()}
        if not statuses or statuses == {"ok"}:
            overall = "ok" if statuses else "unknown"
        elif "ok" in statuses:
            overall = "degraded"
        else:
            overall = "error"

        return {
            "status": overall,
            "collection_enabled": collection_enabled,
            "sensors": sensors,
        }
