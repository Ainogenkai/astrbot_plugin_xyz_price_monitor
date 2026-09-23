"""Public RDAP client for expiry / nameservers / registrar / status (no API key)."""

import asyncio
import time
from datetime import datetime
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .spaceship import USER_AGENT

RDAP_BASE_URL = "https://rdap.org/domain/"
REQUEST_TIMEOUT = 20


def parse_rdap_date(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None


def _extract_registrar(obj: dict[str, Any]) -> str | None:
    for ent in obj.get("entities") or []:
        if not isinstance(ent, dict):
            continue
        roles = ent.get("roles") or []
        if not isinstance(roles, list) or "registrar" not in roles:
            continue
        vcard = ent.get("vcardArray")
        if isinstance(vcard, list) and len(vcard) >= 2 and isinstance(vcard[1], list):
            for item in vcard[1]:
                if isinstance(item, list) and len(item) >= 4 and item[0] == "fn":
                    return str(item[3])
        handle = ent.get("handle")
        if handle:
            return str(handle)
    return None


def _query_rdap_sync(domain: str) -> dict[str, Any]:
    url = RDAP_BASE_URL + quote(domain, safe="")
    req = Request(
        url,
        headers={
            "Accept": "application/rdap+json, application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except HTTPError as exc:
        if exc.code == 404:
            return {
                "domain": domain,
                "registered": False,
                "queried_at": int(time.time()),
            }
        raise

    import json

    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("RDAP 返回的不是 JSON 对象")

    expiration: str | None = None
    registration: str | None = None
    for ev in obj.get("events") or []:
        if not isinstance(ev, dict):
            continue
        action = str(ev.get("eventAction", "")).lower()
        date = ev.get("eventDate")
        if not isinstance(date, str):
            continue
        if action == "expiration":
            expiration = date
        elif action == "registration":
            registration = date

    nameservers: list[str] = []
    for ns in obj.get("nameservers") or []:
        if isinstance(ns, dict) and ns.get("ldhName"):
            nameservers.append(str(ns["ldhName"]).lower())
    nameservers = sorted(set(nameservers))

    status: list[str] = []
    for s in obj.get("status") or []:
        if s:
            status.append(str(s))

    return {
        "domain": domain,
        "registered": True,
        "expiration": expiration,
        "expiration_ts": parse_rdap_date(expiration),
        "registration": registration,
        "nameservers": nameservers,
        "registrar": _extract_registrar(obj),
        "status": status,
        "queried_at": int(time.time()),
    }


async def query_domain(domain: str) -> dict[str, Any]:
    return await asyncio.to_thread(_query_rdap_sync, domain)
