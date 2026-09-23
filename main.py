"""AstrBot plugin: monitor prices of selected six-digit .xyz domains.

Uses the official Spaceship Domain Availability API for price/availability and
optionally auto-registers the bundled `mcp-server-domainwatch` MCP server so the
AstrBot LLM can manage watches conversationally. No third-party packages are
required by the plugin itself.
"""

import asyncio
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from astrbot.api import AstrBotConfig
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from domainwatch import spaceship, store
from domainwatch.spaceship import fmt_price, fmt_status, http_error_message, query_spaceship

DEFAULT_INTERVAL_HOURS = 6
MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 24
DROP_THRESHOLDS = (0.50, 0.67)
MAX_DOMAINS_PER_SESSION = 50

MCP_SERVER_NAME = "domainwatch"
MCP_PACKAGE_SPEC = "mcp-server-domainwatch@0.0.1"
MCP_MANUAL_HINT = (
    "可手动在 AstrBot 面板「插件 → MCP」添加："
    f'command=uvx，args=["{MCP_PACKAGE_SPEC}"]'
)


def _is_six_digit(value: str) -> bool:
    return bool(re.fullmatch(r"\d{6}", value))


def _config_or_env(config: AstrBotConfig, key: str, env_key: str) -> str:
    value = str(config.get(key, "") or "").strip()
    return value or os.getenv(env_key, "").strip()


class XYZPriceMonitor(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_dir: Path = Path(StarTools.get_data_dir())
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_file = self.data_dir / "data.json"
        self.data = store.load_data(self.data_file)
        self.task: asyncio.Task[None] | None = None
        self._check_lock = asyncio.Lock()

    def _save_data(self) -> None:
        store.save_data(self.data_file, self.data)

    def _credentials(self) -> tuple[str, str]:
        return (
            _config_or_env(self.config, "spaceship_api_key", "SPACESHIP_API_KEY"),
            _config_or_env(self.config, "spaceship_api_secret", "SPACESHIP_API_SECRET"),
        )

    def _interval_seconds(self) -> int:
        try:
            hours = int(self.config.get("check_interval_hours", DEFAULT_INTERVAL_HOURS))
        except (TypeError, ValueError):
            hours = DEFAULT_INTERVAL_HOURS
        hours = max(MIN_INTERVAL_HOURS, min(MAX_INTERVAL_HOURS, hours))
        return hours * 3600

    def _session(self, umo: str) -> dict[str, Any]:
        sessions = self.data.setdefault("sessions", {})
        return sessions.setdefault(umo, {"domains": []})

    async def initialize(self):
        await self._ensure_mcp_server()
        self.task = asyncio.create_task(self._monitor_loop())

    async def _ensure_mcp_server(self) -> None:
        """Register the bundled uvx MCP server into AstrBot's MCP runtime (best-effort)."""
        if not bool(self.config.get("enable_mcp", True)):
            return
        try:
            getter = getattr(self.context, "get_llm_tool_manager", None)
            mgr = getter() if callable(getter) else None
            if mgr is None or not hasattr(mgr, "enable_mcp_server"):
                logger.info(f"XYZ监控：当前 AstrBot 不支持插件注册 MCP。{MCP_MANUAL_HINT}")
                return

            env: dict[str, str] = {"DOMAINWATCH_DATA": str(self.data_file)}
            api_key, api_secret = self._credentials()
            if api_key:
                env["SPACESHIP_API_KEY"] = api_key
            if api_secret:
                env["SPACESHIP_API_SECRET"] = api_secret

            server_config: dict[str, Any] = {
                "command": "uvx",
                "args": [MCP_PACKAGE_SPEC],
                "env": env,
            }

            load = getattr(mgr, "load_mcp_config", None)
            save = getattr(mgr, "save_mcp_config", None)
            if callable(load) and callable(save):
                cfg = load() or {}
                servers = cfg.setdefault("mcpServers", {})
                existing = servers.get(MCP_SERVER_NAME)
                if not isinstance(existing, dict):
                    servers[MCP_SERVER_NAME] = server_config
                    save(cfg)
                elif existing.get("command") == "uvx":
                    existing_env = existing.setdefault("env", {})
                    existing_env.update(env)
                    existing["args"] = [MCP_PACKAGE_SPEC]
                    save(cfg)
                    server_config = existing

            await mgr.enable_mcp_server(MCP_SERVER_NAME, server_config)
            logger.info(f"XYZ监控：已注册 MCP 服务器 {MCP_SERVER_NAME}（{MCP_PACKAGE_SPEC}）")
        except Exception as exc:
            logger.warning(
                f"XYZ监控：自动注册 MCP 失败：{type(exc).__name__}: {exc}。{MCP_MANUAL_HINT}"
            )

    async def terminate(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _send(self, umo: str, text: str) -> None:
        await self.context.send_message(umo, MessageChain().message(text))

    async def _query_current(self, domains: list[str]) -> dict[str, dict[str, Any]]:
        api_key, api_secret = self._credentials()
        if not api_key or not api_secret:
            raise RuntimeError("未配置 Spaceship API Key/Secret")
        return await query_spaceship(domains, api_key, api_secret)

    async def _check_all_once(self) -> dict[str, list[str]]:
        async with self._check_lock:
            sessions = self.data.get("sessions", {})
            domains = sorted({
                d for sess in sessions.values()
                for d in sess.get("domains", [])
                if isinstance(d, str) and re.fullmatch(r"\d{6}\.xyz", d)
            })
            if not domains:
                return {}

            results = await self._query_current(domains)
            notices: dict[str, list[str]] = {}
            store_map = self.data.setdefault("domains", {})

            for domain, result in results.items():
                rec = store_map.setdefault(domain, {"history": []})
                old_status = rec.get("last_status")
                old_price = rec.get("last_price")
                new_status = result.get("status")
                new_price = result.get("price")
                alerts: list[str] = []

                if new_status == "available" and old_status in {"taken", "unknown"}:
                    alerts.append(
                        f"🟢 {domain} 现在可注册\n"
                        f"当前注册价：{fmt_price(new_price, result.get('term_years'))}"
                        + ("\n类型：Registry Premium" if result.get("premium") else "\n类型：标准域名")
                    )

                if isinstance(old_price, (int, float)) and isinstance(new_price, (int, float)) and old_price > 0 and new_price < old_price:
                    drop = 1 - float(new_price) / float(old_price)
                    crossed = [x for x in DROP_THRESHOLDS if drop >= x]
                    if crossed:
                        biggest = max(crossed)
                        alerts.append(
                            f"🔥 {domain} 价格大幅下降\n"
                            f"此前：{fmt_price(float(old_price))}\n"
                            f"现在：{fmt_price(float(new_price), result.get('term_years'))}\n"
                            f"下降：{drop * 100:.1f}%（达到 {biggest * 100:.0f}% 阈值）"
                            + ("\n类型：Registry Premium" if result.get("premium") else "\n类型：标准域名")
                        )

                store.apply_result(store_map, domain, result)

                if alerts:
                    for umo, sess in sessions.items():
                        if domain in sess.get("domains", []):
                            notices.setdefault(umo, []).extend(alerts)

            self._save_data()
            return notices

    async def _monitor_once(self) -> None:
        try:
            notices = await self._check_all_once()
        except Exception as exc:
            logger.warning(f"XYZ监控：后台检查失败：{type(exc).__name__}: {exc}")
            return
        for umo, messages in notices.items():
            if messages:
                await self._send(
                    umo,
                    "\n\n".join(dict.fromkeys(messages))
                    + "\n\n数据源：Spaceship 官方 Domain Availability API",
                )

    async def _monitor_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._interval_seconds())
                await self._monitor_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"XYZ监控：后台任务异常：{type(exc).__name__}: {exc}")

    @filter.command("xyz_add")
    async def xyz_add(self, event: AstrMessageEvent, number: str):
        number = (number or "").strip()
        if not _is_six_digit(number):
            yield event.plain_result("格式错误：请输入恰好 6 位数字，例如 /xyz_add 617831")
            return
        domain = f"{number}.xyz"
        session = self._session(event.unified_msg_origin)
        domains = session.setdefault("domains", [])
        if domain not in domains:
            if len(domains) >= MAX_DOMAINS_PER_SESSION:
                yield event.plain_result(f"当前会话最多监控 {MAX_DOMAINS_PER_SESSION} 个域名。")
                return
            domains.append(domain)
        self._save_data()

        try:
            result = (await self._query_current([domain])).get(domain)
            if not result:
                raise RuntimeError("Spaceship 没有返回该域名的结果")
            store.apply_result(self.data.setdefault("domains", {}), domain, result)
            self._save_data()
            kind = "Registry Premium" if result.get("premium") else "标准域名"
            yield event.plain_result(
                f"✅ 已监控 {domain}\n"
                f"状态：{fmt_status(result['status'])}\n"
                f"当前注册价：{fmt_price(result.get('price'), result.get('term_years'))}\n"
                f"类型：{kind}\n"
                f"后台检查：每 {self._interval_seconds() // 3600} 小时\n"
                f"提醒：同一域名价格下降 ≥50% / ≥67%，以及从已注册变为可注册"
            )
        except HTTPError as exc:
            yield event.plain_result(http_error_message(exc))
        except (URLError, TimeoutError) as exc:
            yield event.plain_result(f"❌ 网络请求失败：{type(exc).__name__}: {exc}")
        except Exception as exc:
            yield event.plain_result(f"✅ 已加入 {domain}，但首次查询失败：{type(exc).__name__}: {exc}")

    @filter.command("xyz_remove")
    async def xyz_remove(self, event: AstrMessageEvent, number: str):
        number = (number or "").strip()
        if not _is_six_digit(number):
            yield event.plain_result("格式错误：请输入 6 位数字，例如 /xyz_remove 617831")
            return
        domain = f"{number}.xyz"
        session = self._session(event.unified_msg_origin)
        if domain in session.get("domains", []):
            session["domains"].remove(domain)
            self._save_data()
            yield event.plain_result(f"✅ 已移除 {domain}")
        else:
            yield event.plain_result(f"当前列表里没有 {domain}")

    @filter.command("xyz_list")
    async def xyz_list(self, event: AstrMessageEvent):
        domains = self._session(event.unified_msg_origin).get("domains", [])
        if not domains:
            yield event.plain_result("当前没有监控域名。使用 /xyz_add 617831 添加。")
            return
        lines = ["📋 当前监控："]
        store_map = self.data.get("domains", {})
        for domain in domains:
            rec = store_map.get(domain, {})
            price = rec.get("last_price")
            lines.append(f"{domain}｜{fmt_status(rec.get('last_status', 'unknown'))}｜{fmt_price(price)}")
        yield event.plain_result("\n".join(lines))

    @filter.command("xyz_check")
    async def xyz_check(self, event: AstrMessageEvent):
        domains = self._session(event.unified_msg_origin).get("domains", [])
        if not domains:
            yield event.plain_result("当前没有监控域名。使用 /xyz_add 617831 添加。")
            return
        try:
            results = await self._query_current(domains)
            lines = []
            for domain in domains:
                result = results.get(domain)
                if not result:
                    lines.append(f"{domain}：❌ API 未返回结果")
                    continue
                kind = "Premium" if result.get("premium") else "标准"
                lines.append(
                    f"{domain}：{fmt_status(result['status'])}，"
                    f"{fmt_price(result.get('price'), result.get('term_years'))}，{kind}"
                )
            yield event.plain_result("\n".join(lines))
        except HTTPError as exc:
            yield event.plain_result(http_error_message(exc))
        except (URLError, TimeoutError) as exc:
            yield event.plain_result(f"❌ 网络请求失败：{type(exc).__name__}: {exc}")
        except Exception as exc:
            yield event.plain_result(f"❌ 查询失败：{type(exc).__name__}: {exc}")

    @filter.command("xyz_history")
    async def xyz_history(self, event: AstrMessageEvent, number: str):
        number = (number or "").strip()
        if not _is_six_digit(number):
            yield event.plain_result("格式错误：请输入 6 位数字，例如 /xyz_history 617831")
            return
        domain = f"{number}.xyz"
        rec = self.data.get("domains", {}).get(domain, {})
        history = rec.get("history", [])
        if not history:
            yield event.plain_result(f"暂无 {domain} 的历史记录。")
            return
        lines = [f"📈 {domain} 最近历史："]
        for item in history[-10:]:
            ts = int(item.get("checked_at", 0))
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "未知时间"
            price = fmt_price(item.get("price"), item.get("term_years"))
            lines.append(
                f"{when}｜{fmt_status(item.get('status', 'unknown'))}｜{price}"
                + ("｜Premium" if item.get("premium") else "｜标准")
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("xyz_test")
    async def xyz_test(self, event: AstrMessageEvent):
        try:
            api_key, api_secret = self._credentials()
            if not api_key or not api_secret:
                yield event.plain_result(
                    "❌ 尚未配置 Spaceship API Key/Secret。\n"
                    "请在 AstrBot 的插件配置里填写，或设置环境变量 SPACESHIP_API_KEY / SPACESHIP_API_SECRET。\n"
                    "只需要 domains:read 权限。"
                )
                return
            results = await self._query_current(["617831.xyz"])
            result = results.get("617831.xyz")
            if not result:
                yield event.plain_result("❌ Spaceship API 成功响应，但没有返回 617831.xyz。")
                return
            kind = "Registry Premium" if result.get("premium") else "标准域名"
            yield event.plain_result(
                "✅ Spaceship 官方 API 连接成功\n"
                f"617831.xyz：{fmt_status(result['status'])}\n"
                f"当前注册价：{fmt_price(result.get('price'), result.get('term_years'))}\n"
                f"类型：{kind}\n"
                "此价格可用于该域名自己的历史价格比较。"
            )
        except HTTPError as exc:
            yield event.plain_result(http_error_message(exc))
        except (URLError, TimeoutError) as exc:
            yield event.plain_result(f"❌ 网络请求失败：{type(exc).__name__}: {exc}")
        except Exception as exc:
            yield event.plain_result(f"❌ 测试失败：{type(exc).__name__}: {exc}")
