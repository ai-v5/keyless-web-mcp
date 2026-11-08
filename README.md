# keyless-web-mcp

**Web search and page reading for MCP clients — no API key, no account, no configuration.**

`keyless-web-mcp` is a small [Model Context Protocol](https://modelcontextprotocol.io) server that
gives an agent two tools:

| Tool | What it does |
|---|---|
| `web_search` | Queries several keyless backends in parallel (Google, DuckDuckGo, Bing and Google News by default), interleaves the hits and returns JSON |
| `web_fetch` | Fetches one URL and returns its readable main text (or pretty-printed JSON) |

Both run over stdio, start in well under a second, and work the moment the process starts. There is
nothing to sign up for and no key to paste.

The `google_web` engine renders `google.com/search` in an installed Chrome or Edge, so that one
engine needs a browser on the machine. Every other engine, and `web_fetch`, is pure HTTP: with no
browser present `google_web` simply reports itself unavailable and the rest keep working.

## Why this exists

Most search servers for MCP require either a paid search API key (Brave, Tavily, Serper, Bing…), a
vendor account, or a hosted endpoint your machine may not be able to reach. That is a poor fit when
you just want an agent to look something up:

- **Onboarding is the whole feature.** A key means a signup, a credit card and a config edit before
  the first useful call — for a workflow that is often throwaway.
- **Some networks block the popular search backends** while plain HTML endpoints still work. This
  server tries a direct connection first and can fall back to an explicit proxy without changing
  any tool call.
- **The answers are ordinary web pages.** No proprietary index, no ranking you cannot inspect: the
  scraper is a normal HTTP client, and every result keeps its source URL so it can be cited.

## Install

```bash
# run without installing (recommended)
uvx keyless-web-mcp

# or install it as a tool
pipx install keyless-web-mcp

# or into a project environment
pip install keyless-web-mcp
```

Requires Python 3.10+. The console script `keyless-web-mcp` and `python -m keyless_web` are
equivalent.

## Configure your client

### DeepSeek Harness (DSH)

DSH loads MCP servers from a Cordis profile patch. Add an entry to
`~/.dsh/profiles/<profile>/cordis.patch.yml` (or pass it with `dsh web --patch <file>`):

```yaml
# ~/.dsh/profiles/<profile>/cordis.patch.yml
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

On Windows, if the client does not resolve `uvx` through `PATH`, point `command` at the absolute
executable path (`...\uv.exe` / `...\uvx.exe`).

### Claude Desktop

Edit `claude_desktop_config.json` (`%APPDATA%\Claude\` on Windows, `~/Library/Application Support/Claude/`
on macOS) and restart the app:

```json
{
  "mcpServers": {
    "web": {
      "command": "uvx",
      "args": ["keyless-web-mcp"],
      "env": {
        "MCP_WEB_PROXY": ""
      }
    }
  }
}
```

Any other stdio-capable MCP client works the same way: run `uvx keyless-web-mcp`, `pipx run
keyless-web-mcp` or `python -m keyless_web` as the server command.

## Tools

### `web_search`

| Argument | Type | Default | Notes |
|---|---|---|---|
| `query` | `string` | required | Search terms |
| `max_results` | `integer` | `10` | 1–20 |
| `engines` | `string[]` | configured default | Any of `bing`, `duckduckgo`, `firecrawl`, `google`, `google_web`, `wikipedia` |

Returns a JSON array (a string with `[]` when nothing matched):

```json
[
  {
    "title": "NCCL: Scalable Multi-GPU Communication",
    "url": "https://developer.nvidia.com/blog/...",
    "snippet": "Collective communication primitives for multi-GPU...",
    "engine": "duckduckgo"
  },
  {
    "title": "NVIDIA NCCL 2.27 release notes",
    "url": "https://news.google.com/rss/articles/...",
    "snippet": "...",
    "engine": "google",
    "source": "NVIDIA Developer",
    "date": "Tue, 04 Nov 2026 09:12:00 GMT"
  }
]
```

`source` and `date` are present only for news hits (`engine: "google"`). Engines are queried in the
order you give them; hits are merged and de-duplicated by URL.

### `web_fetch`

| Argument | Type | Default | Notes |
|---|---|---|---|
| `url` | `string` | required | Absolute `http://` or `https://` only |
| `max_chars` | `integer` | `20000` | 500–100000, truncates the returned text |
| `include_links` | `boolean` | `false` | Also list up to 30 links found on the page |

Returns plain text that starts with `Title:` / `URL:` followed by the extracted main content.
JSON endpoints are pretty-printed instead of parsed as HTML.

Both tools report failures as text rather than raising, so the model can react:

```
error[KEYLESS_BAD_URL]: only http:// and https:// URLs are supported (got 'file:///etc/passwd').
Hint: Pass an absolute http:// or https:// URL, for example https://docs.python.org/3/.
```

## Engines

| Engine | Endpoint | Default | Notes |
|---|---|---|---|
| `google_web` | `www.google.com/search` | ✅ first | **The real Google web index**, rendered in a headless Chrome/Edge. A plain HTTP client only receives a JavaScript shell, and the stock `HeadlessChrome` agent earns an "unusual traffic" page; a desktop agent returns normal results. Result URLs are recovered from Google's `/goto` redirector (the `302` it answers, with the `<cite>` breadcrumb as fallback). Costs roughly 4–8 s per search and needs a browser |
| `duckduckgo` | `html.duckduckgo.com/html/` | ✅ second | No-JS HTML endpoint, no key and no cookie; redirect links are unwrapped from `uddg=` |
| `bing` | `www.bing.com/search` | ✅ third | Organic results over plain HTTP; click-tracker links (`/ck/a?...&u=a1<base64url>`) are decoded back to the real URL, and the market (`mkt`/`setlang`) follows the query script so a Chinese query does not come back as English football scores |
| `google` | `news.google.com/rss/search` | ✅ fourth | **Google News RSS, not the web index**: news and tech coverage. Item URLs are Google News article links, which open in a browser but do not return article text to `web_fetch` |
| `wikipedia` | `*.wikipedia.org/w/api.php` | ❌ opt-in | Encyclopedic baseline; stable public API. Sends its own descriptive User-Agent, because Wikimedia answers HTTP 403 to browser-impersonating clients |
| `firecrawl` | `api.firecrawl.dev/v2/search` | ❌ opt-in | A JSON search API rather than a scraped SERP: no key and no account, but rate limited per client address. Useful when the HTML endpoints answer a scripted client with something other than its own results. Snippets are page excerpts, cut at 4000 characters; a 5xx or an empty `200` body is retried twice before it counts as a failure |

Default order: `google_web`, `duckduckgo`, `bing`, `google`. Every engine runs concurrently and the
hits are interleaved round-robin, so one answer can cite several independent sources without any
backend disappearing behind a longer list. A single engine failing never fails the call; if every
engine fails or finds nothing, `web_search` returns `[]`.

## Configuration

Everything is read from the environment, once, at start-up.

| Variable | Default | Meaning |
|---|---|---|
| `MCP_WEB_PROXY` | *(unset)* | Fallback proxy, `host:port` or `http://host:port`. Direct is always tried first |
| `KEYLESS_WEB_ENGINES` | `google_web,duckduckgo,bing,google` | Default engine list (comma separated, unknown names dropped) |
| `KEYLESS_WEB_MIN_INTERVAL` | `1.0` | Minimum seconds between two requests to the same host; `0` disables |
| `KEYLESS_WEB_CACHE_TTL` | `300` | Search result cache TTL in seconds; `0` disables |
| `KEYLESS_WEB_CACHE_TTL_FETCH` | `900` | Fetched page cache TTL in seconds; `0` disables |
| `KEYLESS_WEB_USER_AGENT` | a desktop Chrome UA | `User-Agent` header for scraping, `web_fetch` and the rendered browser |
| `KEYLESS_WEB_TIMEOUT` | `15` | Per-request timeout in seconds |
| `KEYLESS_WEB_BROWSER` | auto-detected | Browser executable used by `google_web`; `off` disables all browser work |
| `KEYLESS_WEB_BROWSER_TIMEOUT` | `25` | Wall-clock budget for one rendered page, in seconds |
| `KEYLESS_WEB_BROWSER_WAIT` | `6000` | Virtual time budget handed to the browser, in milliseconds |

Example:

```bash
MCP_WEB_PROXY=http://127.0.0.1:7890 KEYLESS_WEB_MIN_INTERVAL=2 keyless-web-mcp
```

`KEYLESS_WEB_USER_AGENT` applies to the HTML scrapers and to `web_fetch`. The Wikipedia engine always
identifies itself as `keyless-web-mcp/<version>`, because Wikimedia's
[robot policy](https://meta.wikimedia.org/wiki/User-Agent_policy) requires a descriptive agent and
answers HTTP 403 to browser-impersonating clients.

## Proxy fallback and TLS

Each request is attempted in order:

1. **Direct** — a normal client that honours `HTTP_PROXY`/`HTTPS_PROXY` from the environment.
2. **Explicit proxy** — only when `MCP_WEB_PROXY` is set (for example `http://127.0.0.1:7890`, or
   `10.20.30.40:8080` which is normalized to `http://10.20.30.40:8080`). Plain `host:port` is
   accepted, and an IP works even when local DNS is broken because the proxy resolves hostnames.

Only transport failures (timeout, connect/DNS/TLS error) move to the next backend. Once a response
arrives, its HTTP status is final — 4xx/5xx is reported, not retried through another path. When
every backend fails, all per-backend errors are collected into one `KEYLESS_NETWORK` message, so you
can see which route failed and why.

TLS verification stays **on**. On start-up the server loads the operating system certificate store
via [`truststore`](https://github.com/sethmlarson/truststore), so TLS-interception proxies and
desktop antivirus products that inject their own root CA work without `--insecure` flags or
`REQUESTS_CA_BUNDLE` settings.

## Politeness: rate limiting and caching

- **Per-host minimum interval** (`KEYLESS_WEB_MIN_INTERVAL`, default 1 request/second). The limiter
  keys on host, so hammering one site is throttled while other hosts proceed independently.
- **In-process TTL cache**: search results 300 s, fetched pages 900 s, keyed on engine, query and
  every output-affecting argument. Re-reading the page an agent just found costs nothing.

Both are deliberately conservative: this is a single-user tool, not a crawler. Raise the interval if
you script many queries, and see the compliance note below.

## Errors

Failures are reported to the model as `error[CODE]: message. Hint: ...`.

| Code | When | Typical fix |
|---|---|---|
| `KEYLESS_BAD_URL` | URL is not absolute `http(s)` | Pass a full `https://…` URL |
| `KEYLESS_BAD_REQUEST` | Empty query, unknown engine name, out-of-range argument | Fix the argument (the hint lists valid engines) |
| `KEYLESS_NETWORK` | No backend reached the host, or it answered 4xx/5xx | Check connectivity, retry, or set `MCP_WEB_PROXY` |
| `KEYLESS_RATE_LIMITED` | HTTP 429, or the site refuses scripted access (401/403) | Wait, raise `KEYLESS_WEB_MIN_INTERVAL`, or use another source |
| `KEYLESS_PARSE` | Response arrived but could not be parsed | The site changed its markup or served a block page; retry later |
| `KEYLESS_BROWSER` | `google_web` could not render: no Chrome/Edge, a crash, or the render budget ran out | Install a browser or point `KEYLESS_WEB_BROWSER` at one; the HTTP engines are unaffected |

## Compliance

This project fetches public web pages with an ordinary HTTP client and **no** API agreement with the
sites it reads. By using it you agree to:

- respect each site's Terms of Service, `robots.txt` and rate limits — keep
  `KEYLESS_WEB_MIN_INTERVAL` at a polite value;
- not use it for bulk crawling, scraping behind logins, or republishing copyrighted content;
- treat results as *pointers*, not as licensed data: cite and link the original page.

Scraping HTML endpoints is inherently unstable: an upstream markup change can break an engine
without notice. **No availability or correctness guarantee is offered.** Engines may stop working,
be blocked from your network, or return degraded results; that is a property of the endpoints, not a
bug report target. Two of those degradations look exactly like success, so they are worth naming:
DuckDuckGo's HTML endpoints answer HTTP 202 with an "anomaly" page (no results, no error), and Bing
answers a scripted client with a decoy SERP whose hits belong to *other* queries while the page title
still echoes the requested one. Prefer a backend that reports its own results (`google_web`, or the
`firecrawl` JSON API), and treat a result set you cannot match to your query as untrustworthy.
MIT licensed, provided "as is".

## Development

```bash
git clone https://github.com/ai-v5/keyless-web-mcp
cd keyless-web-mcp
python -m venv .venv
.venv/Scripts/activate        # Windows; use .venv/bin/activate elsewhere
pip install -e ".[dev]"

ruff check .
ruff format --check .
mypy src
pytest -q
```

The architecture keeps policy out of the parsers:

```
src/keyless_web/
├── config.py            # environment -> frozen Settings
├── errors.py            # KEYLESS_* codes, actionable hints
├── cache.py             # TTL cache + cache_key()
├── browser.py           # headless Chrome/Edge -> rendered DOM (--dump-dom)
├── net/transport.py     # direct -> proxy fallback, rate limiting, truststore
├── engines/             # one module per engine, each just (client, query, n)
│   ├── base.py          # SearchResult (pydantic) + Engine protocol
│   ├── duckduckgo.py  bing.py  google_news.py  wikipedia.py  firecrawl.py
│   ├── google_web.py    # parses the rendered Google page, recovers /goto URLs
│   └── __init__.py      # ENGINES registry + parallel run + round-robin merge
├── fetch/extract.py     # trafilatura/BeautifulSoup extraction, JSON passthrough
└── server.py            # MCP tool shell (declarations + argument validation only)
```

Engines never build their own client: they receive an `httpx.Client`, which is why every engine can
be unit-tested with `httpx.MockTransport` and zero network access. The one exception is `google_web`,
which takes its DOM from `browser.dump_dom()` and only uses the client to resolve redirect links --
so its parser is still exercised offline against a fixture.

## Roadmap

Optional next steps, not commitments:

- result scoring (dedupe near-duplicates, rank by title/query overlap) to shrink the JSON the model
  has to read;
- additional keyless engines behind the same registry;
- optional `robots.txt` pre-check for `web_fetch`;
- reuse one browser process across searches instead of one launch per query (the launch dominates
  the ~4–8 s the `google_web` engine costs).

## License

MIT — see [LICENSE](LICENSE). Security policy: [SECURITY.md](SECURITY.md). Contributing:
[CONTRIBUTING.md](CONTRIBUTING.md).

中文文档见 [README.zh-CN.md](README.zh-CN.md)。
