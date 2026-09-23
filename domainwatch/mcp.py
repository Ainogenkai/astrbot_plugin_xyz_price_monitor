"""MCP server exposing domain watch tools (stdio, uvx-friendly).

Run: uvx mcp-server-domainwatch
Spaceship API key/secret are optional (only check_prices needs them).
Data file: $DOMAINWATCH_DATA or ~/.domainwatch/data.json.
"""

import json
import os
import time
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import rdap, spaceship, store

mcp = FastMCP("domainwatch_mcp")


class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


def _data_path() -> Path:
    return store.default_data_path()


def _load() -> dict:
    return store.load_data(_data_path())


def _save(data: dict) -> None:
    store.save_data(_data_path(), data)


def _credentials() -> tuple[str, str]:
    return (
        os.getenv("SPACESHIP_API_KEY", "").strip(),
        os.getenv("SPACESHIP_API_SECRET", "").strip(),
    )


def _norm(value: str) -> str:
    norm = store.normalize_domain(value)
    if not norm:
        raise ValueError(f"无效域名: {value!r}，需要类似 example.com 的完整域名")
    return norm


def _fmt_days_left(days: float) -> str:
    if days is None:
        return "未知"
    if days < 0:
        return f"已过期 {-days:.0f} 天"
    return f"{days:.0f} 天"


def _handle_error(exc: Exception) -> str:
    from urllib.error import HTTPError, URLError

    if isinstance(exc, ValueError):
        return f"Error: {exc}"
    if isinstance(exc, HTTPError):
        return spaceship.http_error_message(exc)
    if isinstance(exc, (URLError, TimeoutError)):
        return f"Error: 网络请求失败：{type(exc).__name__}: {exc}"
    return f"Error: {type(exc).__name__}: {exc}"


@mcp.tool(
    name="domainwatch_watch_domain",
    annotations={
        "title": "Watch Domain",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def watch_domain(
    domain: Annotated[str, Field(description="完整域名，例如 example.com 或 617831.xyz")],
    alert_below: Annotated[
        Optional[float],
        Field(description="价格目标（美元）：最新注册价 ≤ 该值时出现在 domainwatch_triggered_alerts；留空不设"),
    ] = None,
) -> str:
    """把域名加入监控列表，并可选设置价格目标。

    加入后可调用 domainwatch_check_prices 立即查询，
    或 domainwatch_triggered_alerts 查看是否触发提醒。
    """
    domain = _norm(domain)
    if alert_below is not None and alert_below < 0:
        raise ValueError("alert_below 不能为负数")

    data = _load()
    sess = data["sessions"].setdefault(store.MCP_SESSION, {"domains": []})
    domains = sess.setdefault("domains", [])
    existed = domain in domains
    if not existed:
        domains.append(domain)

    rec = data.setdefault("domains", {}).setdefault(domain, {"history": []})
    rec.setdefault("history", [])
    if alert_below is not None:
        rec["alert_below"] = alert_below

    _save(data)

    target = f"，价格目标 ${alert_below:.2f}" if alert_below is not None else ""
    return (
        f"{'已更新' if existed else '已加入'}监控：{domain}{target}\n"
        f"当前监控总数：{len(domains)}\n"
        "提示：调用 domainwatch_check_prices 可立即查询价格与状态。"
    )


@mcp.tool(
    name="domainwatch_unwatch_domain",
    annotations={
        "title": "Unwatch Domain",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def unwatch_domain(
    domain: Annotated[str, Field(description="完整域名，例如 example.com")],
) -> str:
    """把域名从 MCP 监控列表移除（不影响其他会话通过 AstrBot 命令添加的监控）。"""
    domain = _norm(domain)
    data = _load()
    sess = data["sessions"].setdefault(store.MCP_SESSION, {"domains": []})
    domains = sess.setdefault("domains", [])
    if domain not in domains:
        return f"{domain} 不在监控列表中。"
    domains.remove(domain)
    rec = (data.get("domains") or {}).get(domain)
    if isinstance(rec, dict):
        rec.pop("alert_below", None)
    _save(data)
    return f"已移除 {domain}。当前监控总数：{len(domains)}"


@mcp.tool(
    name="domainwatch_list_watches",
    annotations={
        "title": "List Watched Domains",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def list_watches(
    format: Annotated[ResponseFormat, Field(description="markdown（人类可读，默认）或 json（结构化）")] = ResponseFormat.MARKDOWN,
) -> str:
    """列出当前 MCP 监控的全部域名及最新状态、价格、到期剩余天数、价格目标。"""
    data = _load()
    domains = store.session_domains(data, store.MCP_SESSION)
    store_map = data.get("domains") or {}

    items = []
    for domain in domains:
        rec = store_map.get(domain) or {}
        items.append({
            "domain": domain,
            "status": rec.get("last_status"),
            "price": rec.get("last_price"),
            "premium": rec.get("last_premium"),
            "term_years": rec.get("last_term_years"),
            "expiry": rec.get("expiry"),
            "days_left": store.expiry_days_left(rec),
            "alert_below": rec.get("alert_below"),
            "last_checked_at": rec.get("last_checked_at"),
        })

    if not items:
        return "监控列表为空。使用 domainwatch_watch_domain 添加域名。"

    if format == ResponseFormat.JSON:
        return json.dumps({"total": len(items), "items": items}, ensure_ascii=False, indent=2)

    lines = [f"📋 监控列表（{len(items)} 个）："]
    for it in items:
        status = spaceship.fmt_status(it["status"] or "unknown")
        price = spaceship.fmt_price(it["price"], it["term_years"])
        extra = []
        if it["days_left"] is not None:
            extra.append(f"到期剩余 {_fmt_days_left(it['days_left'])}")
        if it["alert_below"] is not None:
            extra.append(f"目标 ≤${it['alert_below']:.2f}")
        suffix = ("｜" + "｜".join(extra)) if extra else ""
        lines.append(f"{it['domain']}｜{status}｜{price}{suffix}")
    return "\n".join(lines)


@mcp.tool(
    name="domainwatch_check_prices",
    annotations={
        "title": "Check Domain Prices",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def check_prices(
    domains: Annotated[
        Optional[list[str]],
        Field(description="要查询的域名列表；留空则查询当前全部 MCP 监控域名"),
    ] = None,
) -> str:
    """通过 Spaceship 官方 API 立即查询域名可用状态与注册价，并更新本地历史。

    需要环境变量 SPACESHIP_API_KEY / SPACESHIP_API_SECRET（domains:read 权限）。
    """
    api_key, api_secret = _credentials()
    if not api_key or not api_secret:
        return (
            "Error: 未配置 SPACESHIP_API_KEY / SPACESHIP_API_SECRET，无法查询价格。"
            "RDAP 类查询（domainwatch_domain_info）不需要 Key。"
        )

    data = _load()
    if domains:
        targets: list[str] = []
        for d in domains:
            targets.append(_norm(d))
    else:
        targets = store.session_domains(data, store.MCP_SESSION)
        if not targets:
            return "监控列表为空，且未指定 domains。"

    try:
        results = await spaceship.query_spaceship(targets, api_key, api_secret)
    except Exception as exc:
        return _handle_error(exc)

    for domain, result in results.items():
        store.apply_result(data.setdefault("domains", {}), domain, result)
    _save(data)

    lines = []
    for domain in targets:
        result = results.get(domain)
        if not result:
            lines.append(f"{domain}：❌ API 未返回结果")
            continue
        kind = "Premium" if result.get("premium") else "标准"
        lines.append(
            f"{domain}：{spaceship.fmt_status(result['status'])}，"
            f"{spaceship.fmt_price(result.get('price'), result.get('term_years'))}，{kind}"
        )
    return "\n".join(lines)


@mcp.tool(
    name="domainwatch_price_history",
    annotations={
        "title": "Domain Price History",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def price_history(
    domain: Annotated[str, Field(description="完整域名")],
    limit: Annotated[int, Field(description="返回最近多少条记录（1-50）", ge=1, le=50)] = 10,
) -> str:
    """查看某个域名本地保存的价格/状态历史记录（来自历次检查）。"""
    domain = _norm(domain)
    data = _load()
    rec = (data.get("domains") or {}).get(domain) or {}
    history = rec.get("history") or []
    if not history:
        return f"暂无 {domain} 的历史记录。可先调用 domainwatch_check_prices。"

    lines = [f"📈 {domain} 最近 {min(limit, len(history))} 条历史："]
    for item in history[-limit:]:
        ts = int(item.get("checked_at", 0))
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "未知时间"
        price = spaceship.fmt_price(item.get("price"), item.get("term_years"))
        lines.append(
            f"{when}｜{spaceship.fmt_status(item.get('status', 'unknown'))}｜{price}"
            + ("｜Premium" if item.get("premium") else "｜标准")
        )
    return "\n".join(lines)


@mcp.tool(
    name="domainwatch_domain_info",
    annotations={
        "title": "Domain RDAP Info",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def domain_info(
    domain: Annotated[str, Field(description="完整域名")],
) -> str:
    """通过公开 RDAP 查询任意域名的到期日、注册商、NS、EPP 状态（免费，无需 API Key）。

    查询结果会缓存进本地监控数据（若该域名在监控列表中，到期信息可用于提醒）。
    RDAP 404 通常表示域名未注册。
    """
    domain = _norm(domain)
    try:
        info = await rdap.query_domain(domain)
    except Exception as exc:
        return _handle_error(exc)

    data = _load()
    store.apply_rdap(data.setdefault("domains", {}), domain, info)
    _save(data)

    if not info.get("registered"):
        return (
            f"**{domain}**\n"
            "- RDAP 无记录（通常表示**未注册/已释放**）\n"
            "- 价格与可注册状态可用 domainwatch_check_prices 确认（需 API Key）"
        )

    rec = (data.get("domains") or {}).get(domain) or {}
    days = store.expiry_days_left(rec)
    days_line = f"{days:.0f} 天" if days is not None else "未知"
    ns = info.get("nameservers") or []
    status = info.get("status") or []
    return (
        f"**{domain}**\n"
        f"- 到期：{info.get('expiration') or '未知'}（剩余 {days_line}）\n"
        f"- 注册商：{info.get('registrar') or '未知'}\n"
        f"- NS：{', '.join(ns) if ns else '未知'}\n"
        f"- 状态：{', '.join(status) if status else '未知'}"
    )


@mcp.tool(
    name="domainwatch_triggered_alerts",
    annotations={
        "title": "Triggered Domain Alerts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def triggered_alerts(
    format: Annotated[ResponseFormat, Field(description="markdown（默认）或 json")] = ResponseFormat.MARKDOWN,
) -> str:
    """汇总当前监控列表中已触发的提醒：价格低于目标、临近到期（≤30/7/1 天）、当前可注册。

    数据来自最近一次检查的本地状态；如需最新价格先调 domainwatch_check_prices。
    """
    data = _load()
    alerts = store.derive_alerts(data)

    if format == ResponseFormat.JSON:
        return json.dumps({"total": len(alerts), "alerts": alerts}, ensure_ascii=False, indent=2)

    if not alerts:
        return "暂无触发提醒。"
    return "🔔 触发提醒：\n" + "\n".join(f"- {a}" for a in alerts)


def main() -> None:
    """Console entry point for `uvx mcp-server-domainwatch` (stdio transport)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
