"""Spaceship official Domain Availability API client (stdlib only)."""

import asyncio
import json
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from . import __version__

SPACESHIP_API_URL = "https://spaceship.dev/api/v1/domains/available"
REQUEST_TIMEOUT = 20
USER_AGENT = f"AstrBot-XYZ-Price-Monitor/{__version__}"


def num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace("$", "").replace(",", "").strip())
        except ValueError:
            return None
    return None


def _post_json(url: str, payload: dict[str, Any], api_key: str, api_secret: str) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        method="POST",
        headers={
            "X-Api-Key": api_key,
            "X-Api-Secret": api_secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", "replace")
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("Spaceship API 返回的不是 JSON 对象")
    return obj


def parse_item(item: dict[str, Any]) -> dict[str, Any]:
    domain = str(item.get("domain", "")).lower()
    result = str(item.get("result", "unexpectedError"))
    premium_pricing = item.get("premiumPricing")
    if not isinstance(premium_pricing, list):
        premium_pricing = []

    # Spaceship documents price for available standard/premium names. For
    # premium names, premiumPricing[register].price is the exact registration
    # quote; for standard names, price.amount is the payable total for the
    # shortest permitted term.
    price_obj = item.get("price")
    price: float | None = None
    term_years: float | None = None
    is_premium = False

    if isinstance(price_obj, dict):
        price = num(price_obj.get("amount"))
        term_years = num(price_obj.get("pricedYears"))
        is_premium = bool(price_obj.get("isPremium"))

    for p in premium_pricing:
        if not isinstance(p, dict):
            continue
        if str(p.get("operation", "")).lower() == "register":
            pp = num(p.get("price"))
            if pp is not None:
                price = pp
            is_premium = True
            break

    if price is None and isinstance(item.get("premiumPricing"), list):
        for p in item["premiumPricing"]:
            if isinstance(p, dict):
                pp = num(p.get("price"))
                if pp is not None:
                    price = pp
                    is_premium = True
                    break

    if result == "available":
        status = "available"
    elif result == "taken":
        status = "taken"
    elif result == "tldNotSupported":
        status = "unsupported"
    else:
        status = "unknown"

    return {
        "domain": domain,
        "status": status,
        "price": round(price, 4) if price is not None else None,
        "premium": is_premium,
        "term_years": term_years,
        "checked_at": int(time.time()),
    }


async def query_spaceship(
    domains: list[str], api_key: str, api_secret: str
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for start in range(0, len(domains), 20):
        batch = domains[start:start + 20]
        payload = {"domains": batch}
        response = await asyncio.to_thread(
            _post_json,
            SPACESHIP_API_URL,
            payload,
            api_key,
            api_secret,
        )
        items = response.get("domains")
        if not isinstance(items, list):
            raise ValueError("Spaceship API 未返回 domains 数组")
        for item in items:
            if isinstance(item, dict):
                parsed = parse_item(item)
                if parsed["domain"]:
                    results[parsed["domain"]] = parsed
    return results


def fmt_price(price: float | None, term_years: float | None = None) -> str:
    if price is None:
        return "未知"
    if term_years is not None and term_years != 1:
        if float(term_years).is_integer():
            return f"${price:.2f} / {int(term_years)}年"
        return f"${price:.2f} / {term_years:g}年"
    return f"${price:.2f}/年"


def fmt_status(status: str) -> str:
    return {
        "available": "🟢 可注册",
        "taken": "🔴 已注册",
        "unsupported": "⚪ 不支持",
    }.get(status, "🟡 未确认")


def http_error_message(exc: HTTPError) -> str:
    if exc.code == 401:
        return "❌ Spaceship API 认证失败（401）。请检查 API Key 和 API Secret。"
    if exc.code == 403:
        return "❌ Spaceship API 无权限（403）。请确认 Key 至少拥有 domains:read 权限。"
    if exc.code == 429:
        return "❌ Spaceship API 限流（429）。请降低检查频率或减少并发请求。"
    return f"❌ Spaceship API HTTP {exc.code}: {exc.reason}"
