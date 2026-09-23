"""JSON watchlist / history / alert store shared by plugin and MCP server."""

import json
import os
import re
import time
from pathlib import Path
from typing import Any

DATA_VERSION = 4
MCP_SESSION = "mcp"
MAX_HISTORY_PER_DOMAIN = 180
EXPIRY_ALERT_DAYS = (30, 7, 1)

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$"
)


def default_data_path() -> Path:
    env = os.getenv("DOMAINWATCH_DATA", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".domainwatch" / "data.json"


def empty_data() -> dict[str, Any]:
    return {"version": DATA_VERSION, "sessions": {}, "domains": {}}


def load_data(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_data()
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(obj, dict) and isinstance(obj.get("domains"), dict):
            obj["version"] = DATA_VERSION
            obj.setdefault("sessions", {})
            return obj
    except Exception:
        pass
    return empty_data()


def save_data(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def normalize_domain(value: str) -> str:
    v = (value or "").strip().lower()
    for prefix in ("https://", "http://"):
        if v.startswith(prefix):
            v = v[len(prefix):]
    v = v.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    v = v.split(":", 1)[0].strip().rstrip(".")
    if not _DOMAIN_RE.fullmatch(v):
        return ""
    return v


def apply_result(store: dict[str, Any], domain: str, result: dict[str, Any]) -> dict[str, Any]:
    rec = store.setdefault(domain, {"history": []})
    history = rec.setdefault("history", [])
    history.append({
        "checked_at": result.get("checked_at", int(time.time())),
        "status": result.get("status"),
        "price": result.get("price"),
        "premium": bool(result.get("premium")),
        "term_years": result.get("term_years"),
    })
    del history[:-MAX_HISTORY_PER_DOMAIN]

    rec["last_status"] = result.get("status")
    if isinstance(result.get("price"), (int, float)):
        rec["last_price"] = result["price"]
    rec["last_premium"] = bool(result.get("premium"))
    rec["last_term_years"] = result.get("term_years")
    rec["last_checked_at"] = result.get("checked_at")
    return rec


def apply_rdap(store: dict[str, Any], domain: str, info: dict[str, Any]) -> dict[str, Any]:
    rec = store.setdefault(domain, {"history": []})
    rec.setdefault("history", [])
    if info.get("registered"):
        rec["expiry"] = info.get("expiration")
        rec["expiry_ts"] = info.get("expiration_ts")
        rec["last_nameservers"] = info.get("nameservers") or []
        rec["last_registrar"] = info.get("registrar")
        rec["last_rdap_status"] = info.get("status") or []
    rec["rdap_registered"] = bool(info.get("registered"))
    rec["last_rdap_checked_at"] = info.get("queried_at")
    return rec


def session_domains(data: dict[str, Any], session_key: str) -> list[str]:
    sessions = data.get("sessions") or {}
    sess = sessions.get(session_key) or {}
    domains = sess.get("domains") or []
    return [d for d in domains if isinstance(d, str)]


def all_watched_domains(data: dict[str, Any]) -> list[str]:
    found: set[str] = set()
    for sess in (data.get("sessions") or {}).values():
        if not isinstance(sess, dict):
            continue
        for d in sess.get("domains") or []:
            if isinstance(d, str):
                found.add(d)
    return sorted(found)


def expiry_days_left(rec: dict[str, Any], now: float | None = None) -> float | None:
    ts = rec.get("expiry_ts")
    if not isinstance(ts, (int, float)):
        return None
    if now is None:
        now = time.time()
    return (float(ts) - now) / 86400.0


def derive_alerts(data: dict[str, Any], now: float | None = None) -> list[str]:
    alerts: list[str] = []
    watched = set(all_watched_domains(data))
    store = data.get("domains") or {}
    for domain in sorted(watched):
        rec = store.get(domain)
        if not isinstance(rec, dict):
            continue

        target = rec.get("alert_below")
        price = rec.get("last_price")
        if isinstance(target, (int, float)) and isinstance(price, (int, float)) and price <= target:
            alerts.append(
                f"{domain}: 当前价 ${price:.2f} 已低于目标 ${float(target):.2f}"
            )

        days = expiry_days_left(rec, now)
        if days is not None and days >= 0:
            for th in EXPIRY_ALERT_DAYS:
                if days <= th:
                    alerts.append(
                        f"{domain}: 还有 {days:.0f} 天到期"
                        + (f"（{rec.get('expiry')}）" if rec.get("expiry") else "")
                    )
                    break

        if rec.get("last_status") == "available":
            from .spaceship import fmt_price

            alerts.append(
                f"{domain}: 现在可注册，{fmt_price(rec.get('last_price'), rec.get('last_term_years'))}"
            )
    return alerts
