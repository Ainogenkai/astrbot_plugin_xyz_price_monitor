<!-- mcp-name: io.github.Ainogenkai/mcp-server-domainwatch -->

# astrbot_plugin_xyz_price_monitor

六位 .xyz 域名注册价格监控系统。单一仓库包含两个可独立使用的部分：

| 部分 | 形态 | 用途 |
|---|---|---|
| AstrBot 插件 | AstrBot 插件（zip / 插件市场安装） | 六位 .xyz 域名监控、后台定时检查、价格告警推送 |
| `mcp-server-domainwatch` | PyPI 包 | MCP Server（stdio），供 Claude Desktop 等 MCP 客户端以自然语言查询价格与注册信息 |

两部分共享同一套核心代码（`domainwatch/`）与同一份本地 JSON 数据：价格与状态来自 Spaceship 官方 API，注册信息来自公开 RDAP，所有数据仅保存在本机。

## 功能

- 六位 .xyz 域名监控列表，按 AstrBot 会话隔离，单会话最多 50 个域名
- 后台定时检查（默认 6 小时，可配置 1–24 小时），触发规则命中时推送到相应对话
- 内置告警规则：价格较上次检查下降 ≥50% 或 ≥67%；域名由「已注册/未知」变为「可注册」
- 价格历史自动记录（每域名最多 180 条）
- MCP Server 提供 7 个工具：添加/移除监控、查看监控列表、批量查价、价格历史、RDAP 注册信息、告警汇总
- 自然语言使用：在 MCP 客户端中直接说「帮我监控 617831.xyz，低于 150 美元提醒我」
- Spaceship API Key/Secret 经 AstrBot 凭据机制加密存储（或走环境变量）；RDAP 查询免费、无需 Key

## 架构

```text
AstrBot 进程
├── xyz-price-monitor 插件
│   ├── /xyz_* 命令（添加 / 移除 / 列表 / 查价 / 历史 / 测试）
│   ├── 后台定时检查 → 触发时推送到相应对话会话
│   └── 加载时自动注册 MCP 服务器（uvx 拉起，注入 Key 与数据路径）
└── MCP Client（AstrBot 内置）
    └── AI 助手 ←MCP (stdio)→ mcp-server-domainwatch 子进程
                                ├── 7 个 domainwatch_* 工具
                                ├── Spaceship API（状态与报价）
                                ├── rdap.org（注册信息）
                                └── data.json（本地数据）
```

## 安装与配置

### AstrBot 插件

前置要求：

- AstrBot ≥ 4.23.0
- AstrBot 运行环境（宿主机或容器内）已安装 `uv`（提供 `uvx`）。缺少时插件降级为仅命令模式，全部 `/xyz_*` 命令可用，但无法自动注册 MCP
- Spaceship API Key 与 Secret（免费注册，`domains:read` 权限即可）

步骤：

1. 安装插件（插件市场或本地安装 zip）
2. 在插件配置页填写 API Key/Secret（加密存储；留空时回退到环境变量 `SPACESHIP_API_KEY` / `SPACESHIP_API_SECRET`）
3. 启动或重载 AstrBot，日志出现 `已注册 MCP 服务器 domainwatch` 即表示 MCP 服务器就绪
4. 发送 `/xyz_test` 验证 Spaceship API 连通性

插件配置项：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `spaceship_api_key` | 空 | Spaceship API Key；留空使用环境变量 `SPACESHIP_API_KEY` |
| `spaceship_api_secret` | 空 | Spaceship API Secret；留空使用环境变量 `SPACESHIP_API_SECRET` |
| `check_interval_hours` | 6 | 后台检查间隔（小时），建议 1–24 |
| `enable_mcp` | true | 启动时是否自动注册 MCP 服务器 |

自动注册失败时（日志出现「自动注册 MCP 失败」），在 AstrBot 面板「插件 → MCP」手动添加：

```json
{
  "mcpServers": {
    "domainwatch": {
      "command": "uvx",
      "args": ["mcp-server-domainwatch@0.0.1"],
      "env": {
        "SPACESHIP_API_KEY": "sk-...",
        "SPACESHIP_API_SECRET": "..."
      }
    }
  }
}
```

若需让手动注册的 MCP 与插件共享同一份数据，在 `env` 中追加 `"DOMAINWATCH_DATA": "<插件数据目录>/data.json"`；不追加时 MCP 使用独立的 `~/.domainwatch/data.json`。

### 独立使用（不装 AstrBot）

`mcp-server-domainwatch` 可单独使用。例如 Claude Desktop 的 `claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "domainwatch": {
      "command": "uvx",
      "args": ["mcp-server-domainwatch@0.0.1"],
      "env": {
        "SPACESHIP_API_KEY": "sk-...",
        "SPACESHIP_API_SECRET": "..."
      }
    }
  }
}
```

- 首次 `uvx` 会从 PyPI 下载安装（数秒），之后使用本地 uv 缓存
- 价格查询需要 Key/Secret；RDAP 注册信息查询（`domainwatch_domain_info`）不需要
- 独立运行时数据默认存于 `~/.domainwatch/data.json`，与插件端数据互不干扰

## 使用

### 插件命令

命令作用于当前会话，各会话维护独立的监控列表（最多 50 个域名）。

| 命令 | 说明 |
|---|---|
| `/xyz_add <6位数字>` | 添加域名监控，并立即返回当前状态与价格 |
| `/xyz_remove <6位数字>` | 从当前会话移除该域名 |
| `/xyz_list` | 当前会话监控列表及各域名最新状态、价格 |
| `/xyz_check` | 立即查询当前会话全部域名的状态与价格（实时查询，不写入历史） |
| `/xyz_history <6位数字>` | 该域名最近 10 条价格历史 |
| `/xyz_test` | 测试 Spaceship API 连通性（查询 617831.xyz） |

告警推送：后台任务按 `check_interval_hours` 间隔检查全部已监控域名，命中以下规则时推送到监控该域名的会话——

- 状态由「已注册 / 未知」变为「可注册」（附当前注册价）
- 价格较上次检查下降 ≥50% 或 ≥67%（附下降幅度）

### MCP 工具

| 工具 | 说明 |
|---|---|
| `domainwatch_watch_domain` | 添加域名到 MCP 监控列表，可选设置价格目标（美元） |
| `domainwatch_unwatch_domain` | 从 MCP 监控列表移除域名 |
| `domainwatch_list_watches` | 列出全部监控域名及最新状态、价格、到期剩余天数、价格目标（markdown / json） |
| `domainwatch_check_prices` | 通过 Spaceship API 批量查询状态与价格并更新历史；不带参数时查询 MCP 监控列表 |
| `domainwatch_price_history` | 查看某域名的本地价格历史（最近 1–50 条） |
| `domainwatch_domain_info` | 通过公开 RDAP 查询任意域名的到期日、注册商、NS、EPP 状态（无需 Key） |
| `domainwatch_triggered_alerts` | 汇总告警：价格 ≤ 目标价、临近到期（≤30/7/1 天）、当前可注册 |

MCP 端是无状态的「问一句答一句」：不跑后台轮询、不主动推送消息；定时检查与推送由插件端负责。两端写入同一份 `data.json`，价格历史与到期信息互相同步（监控列表本身按会话/来源分桶）。

自然语言示例（MCP 客户端中直接说）：

- 「帮我监控 617831.xyz，低于 150 美元提醒我」→ `domainwatch_watch_domain`
- 「查一下 617831.xyz 和 123456.xyz 现在的价格」→ `domainwatch_check_prices`
- 「google.com 什么时候到期？注册商是谁？」→ `domainwatch_domain_info`
- 「现在有哪些触发提醒？」→ `domainwatch_triggered_alerts`

## 数据与告警

- 存储：单文件 JSON。插件端位于插件数据目录下的 `data.json`；独立 MCP 默认 `~/.domainwatch/data.json`；插件自动注册 MCP 时通过 `DOMAINWATCH_DATA` 环境变量注入插件端路径，使两端共享同一文件
- 结构（schema v4）：`sessions`（按会话/来源分组的监控列表）+ `domains`（各域名的历史与最新状态，历史每域名上限 180 条）
- 写入采用临时文件 + 原子重命名，避免半写损坏
- 插件端告警（降价 50%/67%、变为可注册）为固定规则；`alert_below` 是 MCP 端添加监控时的可选价格目标，最新价 ≤ 目标价时出现在 `domainwatch_triggered_alerts` 汇总中

## 数据来源

- 状态与价格：Spaceship 官方 API `POST /api/v1/domains/available`（批量可用性与报价，不产生订单）。标准域名取最短允许注册年限的应付总额（USD）；Premium 域名取注册操作的精确报价。该报价来自 Spaceship 注册商，与其他注册商或促销期价格可能不同
- 注册信息：rdap.org（RFC 7946 bootstrap，自动重定向到对应注册局）；RDAP 无记录通常表示域名未注册

## 运行要求

| 组件 | 要求 |
|---|---|
| AstrBot（插件） | ≥ 4.23.0 |
| Python（MCP Server） | ≥ 3.10 |
| `uv` / `uvx` | 自动注册 MCP 需要；缺失时插件降级为仅命令模式 |
| Spaceship | API Key + Secret，`domains:read` 权限，免费账号 |

## 隐私

全部数据（监控列表、价格、历史、注册信息）仅保存在本机；本项目的网络通信只有对 Spaceship API 与 RDAP 的查询，无任何遥测或第三方上报。API Key/Secret 经 AstrBot 凭据机制加密存储（插件方式），或经环境变量传给 MCP 子进程（独立方式）。

## 开发

```text
astrbot_plugin_xyz_price_monitor/
├── main.py               # AstrBot 插件主体（命令、后台任务、MCP 自动注册）
├── metadata.yaml         # AstrBot 插件元数据
├── _conf_schema.json     # 插件配置 schema
├── pyproject.toml        # mcp-server-domainwatch 包定义
└── domainwatch/          # 共享核心（插件与 MCP server 共同引用）
    ├── spaceship.py      # Spaceship API 客户端
    ├── rdap.py           # RDAP 查询
    ├── store.py          # JSON 存储与告警推导
    └── mcp.py            # MCP Server（7 个工具，stdio）
```

本地运行 MCP server 测试：

```bash
uv lock
uv sync
uv run python -m domainwatch.mcp
```

## License

GPL-3.0-only，见 [LICENSE](https://github.com/Ainogenkai/astrbot_plugin_xyz_price_monitor/blob/main/LICENSE)。
