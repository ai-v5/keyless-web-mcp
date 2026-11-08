# keyless-web-mcp（中文说明）

**给 MCP 客户端的网页搜索与正文抓取工具：不需要 API key、不需要账号、不需要配置。**

本项目是一个基于 stdio 的 [Model Context Protocol](https://modelcontextprotocol.io) 服务器，向模型提供两个工具：

| 工具 | 作用 |
|---|---|
| `web_search` | 并行查询 Google / DuckDuckGo / Bing / Google News，轮转交织合并后返回 JSON |
| `web_fetch` | 抓取一个 URL，返回可读正文（JSON 接口则美化输出） |

`google_web` 引擎用本机的 Chrome 或 Edge 渲染 `google.com/search`，只有它需要浏览器；其余引擎与
`web_fetch` 都是纯 HTTP——机器上没有浏览器时 `google_web` 自行退场，其他引擎照常工作。

## 为什么需要它

多数带搜索能力的 MCP 服务器要求付费搜索 API（Brave、Tavily、Serper、Bing 等）、厂商账号，或一个你的机器未必连得上的托管端点。只想让 agent 顺手查一下资料时，这套前置成本并不划算：

- **零配置就是全部卖点**：不需要注册、不需要填卡号、不需要改配置就能打第一个查询。
- **部分网络会挡掉主流搜索后端**，但普通 HTML 端点仍然可用。本服务器**先直连、失败再退回显式代理**，工具调用本身不用改。
- **结果是普通网页**：没有专有索引、没有不可解释的排序，每条结果都带来源 URL，可直接引用。

## 安装

```bash
uvx keyless-web-mcp                # 不安装直接跑（推荐）
pipx install keyless-web-mcp       # 或装成命令行工具
pip install keyless-web-mcp        # 或装进项目环境
```

需要 Python 3.10+；`keyless-web-mcp` 与 `python -m keyless_web` 等价。

## 客户端配置

### DeepSeek Harness（DSH）

DSH 从 Cordis 配置补丁加载 MCP 服务器，在 `~/.dsh/profiles/<profile>/cordis.patch.yml` 中加入（也可用 `dsh web --patch <文件>` 指定）：

```yaml
- insert:
    - id: keyless-web
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: web
        transport: stdio
        command: uvx
        args: ['keyless-web-mcp']
        env:
          MCP_WEB_PROXY: ''
        toolCallTimeoutMs: 120000
```

Windows 下若客户端不解析 `PATH` 里的 `uvx`，把 `command` 改成 `uv.exe` / `uvx.exe` 的绝对路径即可。

### Claude Desktop

编辑 `claude_desktop_config.json`（Windows：`%APPDATA%\Claude\`；macOS：`~/Library/Application Support/Claude/`）后重启：

```json
{
  "mcpServers": {
    "web": {
      "command": "uvx",
      "args": ["keyless-web-mcp"],
      "env": { "MCP_WEB_PROXY": "" }
    }
  }
}
```

其他 stdio 型 MCP 客户端同理：把 `uvx keyless-web-mcp`、`pipx run keyless-web-mcp` 或
`python -m keyless_web` 当作服务器启动命令。

## 工具参数

**`web_search`**

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `query` | string | 必填 | 查询词 |
| `max_results` | int | 10 | 1–20 |
| `engines` | string[] | 配置的默认列表 | 取值：`google_web` / `firecrawl` / `duckduckgo` / `bing` / `google` / `wikipedia` |

返回 JSON 数组（无结果时返回 `[]`），字段为 `title`、`url`、`snippet`、`engine`；新闻结果额外带 `source`、`date`。所有引擎并发执行，结果按 URL 去重后**轮转交织**，一次查询能同时引用多个独立来源，也不会让某个引擎的结果被长列表挤掉。

**`web_fetch`**

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `url` | string | 必填 | 仅接受绝对的 `http://` / `https://` |
| `max_chars` | int | 20000 | 500–100000，超出截断 |
| `include_links` | bool | false | 额外列出页面内最多 30 条链接 |

返回以 `Title:` / `URL:` 开头的可读正文；JSON 接口做格式化输出。

出错时返回可操作文本而不是抛异常：

```
error[KEYLESS_BAD_URL]: only http:// and https:// URLs are supported (got 'file:///etc/passwd').
Hint: Pass an absolute http:// or https:// URL, for example https://docs.python.org/3/.
```

## 引擎说明

| 引擎 | 端点 | 默认 | 说明 |
|---|---|---|---|
| `google_web` | `www.google.com/search` | ✅ 第一 | **真正的 Google 网页索引**，用无头 Chrome/Edge 渲染。纯 HTTP 客户端只能拿到 JS 外壳，而自带 `HeadlessChrome` UA 会吃到「unusual traffic」拦截页；换成桌面 UA 就正常。结果 URL 从 Google 的 `/goto` 跳转恢复（读它的 `302`，拿不到时用 `<cite>` 面包屑兜底）。每次约 4–8 秒，且需要本机有浏览器 |
| `duckduckgo` | `html.duckduckgo.com/html/` | ✅ 第二 | 无 JS 的 HTML 端点，免 key 免 cookie；跳转链接会解 `uddg=` |
| `bing` | `www.bing.com/search` | ✅ 第三 | 纯 HTTP 就能拿自然结果；点击包装链接（`/ck/a?...&u=a1<base64url>`）会解码回真实 URL，市场参数（`mkt`/`setlang`）跟随查询语言，中文查询不会变成英文足球比分 |
| `google` | `news.google.com/rss/search` | ✅ 第四 | **是 Google News RSS，不是网页索引**，偏向新闻/技术报道；条目 URL 是 Google 新闻跳转链接，浏览器能打开，但 `web_fetch` 抓不到正文 |
| `wikipedia` | `*.wikipedia.org/w/api.php` | ❌ 需显式指定 | 百科类兜底，公共 API 稳定；**自带**描述性 User-Agent，因为 Wikimedia 对伪装浏览器的客户端直接返回 403 |
| `firecrawl` | `api.firecrawl.dev/v2/search` | ❌ 需显式指定 | 不抓 SERP，直接调 JSON 搜索接口：免 key 免注册，但按客户端 IP 限流。适合 HTML 端点对脚本客户端不给自己结果的网络环境；`snippet` 是页面摘录，截断到 4000 字符；遇到 5xx 或空 `200` body 会重试两次再判失败 |

默认顺序：`google_web` → `duckduckgo` → `bing` → `google`。单个引擎失败不影响整体，其余引擎结果照常返回；全部失败或全部无结果时返回 `[]`。

## 配置项（环境变量）

| 变量 | 默认 | 含义 |
|---|---|---|
| `MCP_WEB_PROXY` | 未设置 | 兜底代理，`host:port` 或 `http://host:port`；始终先直连 |
| `KEYLESS_WEB_ENGINES` | `google_web,duckduckgo,bing,google` | 默认引擎列表（逗号分隔，未知名字丢弃） |
| `KEYLESS_WEB_MIN_INTERVAL` | `1.0` | 同一 host 两次请求的最小间隔（秒），`0` 关闭 |
| `KEYLESS_WEB_CACHE_TTL` | `300` | 搜索结果缓存 TTL（秒），`0` 关闭 |
| `KEYLESS_WEB_CACHE_TTL_FETCH` | `900` | 抓取页面缓存 TTL（秒），`0` 关闭 |
| `KEYLESS_WEB_USER_AGENT` | 桌面 Chrome UA | 抓取、`web_fetch` 与无头浏览器使用的 `User-Agent`（Wikipedia 引擎除外，见文末说明） |
| `KEYLESS_WEB_TIMEOUT` | `15` | 单请求超时（秒） |
| `KEYLESS_WEB_BROWSER` | 自动探测 | `google_web` 使用的浏览器可执行文件；填 `off` 关闭所有浏览器能力 |
| `KEYLESS_WEB_BROWSER_TIMEOUT` | `25` | 单个页面渲染的墙钟预算（秒） |
| `KEYLESS_WEB_BROWSER_WAIT` | `6000` | 交给浏览器的虚拟时间预算（毫秒） |

`KEYLESS_WEB_USER_AGENT` 作用于 HTML 抓取引擎与 `web_fetch`。Wikipedia 引擎始终以
`keyless-web-mcp/<版本>` 自报身份：Wikimedia 的
[机器人政策](https://meta.wikimedia.org/wiki/User-Agent_policy) 要求描述性 UA，对伪装浏览器的客户端返回 403。

## 代理回退与 TLS

每个请求依次尝试：**直连**（尊重 `HTTP_PROXY`/`HTTPS_PROXY`）→ 配置了 `MCP_WEB_PROXY` 时的**显式代理**。
只有超时/连接类错误才换后端；一旦拿到响应，4xx/5xx 直接如实上报、不再换路重试。全部后端失败时会把各后端错误聚合成一条 `KEYLESS_NETWORK` 消息，便于判断是哪条路挂了。

TLS 校验始终保持开启：启动时用 [`truststore`](https://github.com/sethmlarson/truststore) 把操作系统证书库注入 Python 的 TLS 栈，企业 TLS 拦截或杀软替换的根证书无需 `--insecure` 或 `REQUESTS_CA_BUNDLE`。

## 限速与礼貌抓取

- **按 host 最小间隔**（`KEYLESS_WEB_MIN_INTERVAL`，默认 1 请求/秒）：压一个站点会被限速，其他 host 不受影响。
- **进程内 TTL 缓存**：搜索 300 s、页面 900 s，键包含引擎、查询与所有影响输出的参数。

两者都刻意保守：这是给单个用户用的工具，不是爬虫。批量脚本请调大间隔。

## 错误码

| 错误码 | 触发场景 | 建议 |
|---|---|---|
| `KEYLESS_BAD_URL` | 非绝对 http(s) URL | 传完整 `https://…` |
| `KEYLESS_BAD_REQUEST` | 空查询、未知引擎名、参数越界 | 按提示修正参数（Hint 会列出合法引擎） |
| `KEYLESS_NETWORK` | 所有后端都连不上，或对端返回 4xx/5xx | 检查网络后重试，或设置 `MCP_WEB_PROXY` |
| `KEYLESS_RATE_LIMITED` | HTTP 429，或站点拒绝脚本访问（401/403） | 等待、调大 `KEYLESS_WEB_MIN_INTERVAL`，或换来源 |
| `KEYLESS_PARSE` | 响应到达但无法解析 | 站点改版或返回拦截页，稍后重试 |
| `KEYLESS_BROWSER` | `google_web` 渲染失败：没有 Chrome/Edge、浏览器崩溃或渲染预算用尽 | 装浏览器或用 `KEYLESS_WEB_BROWSER` 指定可执行文件；HTTP 引擎不受影响 |

## 合规提示

本项目用普通 HTTP 客户端抓取公开网页，**与这些站点没有任何 API 协议**。使用即表示你同意：

- 遵守目标站点的服务条款、`robots.txt` 与访问频率限制，把 `KEYLESS_WEB_MIN_INTERVAL` 保持在礼貌值；
- 不用于批量爬取、登录后抓取或转载受版权保护的内容；
- 把结果当作**线索**而非授权数据，引用时给出原文链接。

抓取 HTML 端点天然不稳定：上游改版可能随时让某个引擎失效。**本项目不保证可用性与结果正确性**，引擎失效、被网络阻断或结果降级属于端点特性，不作为缺陷修复承诺。有两种降级会伪装成正常成功，值得点名：DuckDuckGo 的 HTML 端点用 **HTTP 202 + anomaly 页**应答（没有结果，也不报错）；Bing 对脚本客户端返回**诱饵 SERP**——页面标题回显你的查询，结果却属于别的查询（实测会出现体育站、成人站、无关电商页）。因此优先选能报出自己结果的通道（`google_web`，或 `firecrawl` 这类 JSON API），并且不要把和自己查询对不上的结果当真。MIT 许可，按“现状”提供。

## 开发

```bash
git clone https://github.com/ai-v5/keyless-web-mcp
cd keyless-web-mcp
python -m venv .venv
pip install -e ".[dev]"
ruff check .
ruff format --check .
mypy src
pytest -q
```

分层约定：引擎只接收 `httpx.Client`，重试/代理/限速/缓存全部在 `net/transport.py` 与上层，因此每个引擎都能用 `httpx.MockTransport` 离线单测。唯一例外是 `google_web`：它的 DOM 来自
`browser.dump_dom()`，只在解析后用同一个客户端去解 `/goto` 跳转，所以解析器依然可以离线测。

## Roadmap（可选，不作承诺）

- 结果评分：近似去重 + 按标题/查询重合度排序，压缩模型需要读的 JSON；
- 同一注册表下增加更多 keyless 引擎；
- `web_fetch` 增加可选 `robots.txt` 预检查；
- 复用一个常驻浏览器进程，而不是每次查询都拉起一次（`google_web` 那 4–8 秒基本都花在启动上）。

## 许可

MIT，见 [LICENSE](LICENSE)；安全策略见 [SECURITY.md](SECURITY.md)；贡献指南见 [CONTRIBUTING.md](CONTRIBUTING.md)。
