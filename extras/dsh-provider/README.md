# DSH web provider

Plug this package into [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
as a **web provider** instead of an MCP server.

The difference matters:

| | MCP server (`keyless-web-mcp` command) | Web provider (this directory) |
|---|---|---|
| Tools it creates | `mcp__<server>__web_search`, `..._web_fetch` — a second namespace next to the built-in ones | none: it fills the harness's **own** `web_search` / `web_fetch`, so there is one tool, not two |
| Where the provider choice lives | client config (`mcpServers` / cordis `insert`) | the `web` plugin row: `searchProvider` / `fetchProvider` |
| Extra features of the seam | — | source cards, truncation flags and abort handling the harness already implements |

Both paths run the same Python code. Use MCP for portability across clients, the
provider for a native feel inside DSH.

## How it works

`keyless-web-provider.mjs` registers one search and one fetch provider with
`ctx.web` and owns **one long-lived** worker process:

```
DSH  ──provider──▶  keyless-web-provider.mjs  ──JSON lines over stdio──▶  python -m keyless_web.worker
```

Starting an interpreter per call would cost more than the search itself, since
importing `trafilatura` dominates a cold process. The worker is restarted on the
next call if it dies; a worker that keeps crashing, or an interpreter that cannot
import the package, ends as `available() === false` rather than throwing on every
tool call.

## Install

```bash
# the interpreter that will serve the calls must carry the worker module
uv tool install git+https://github.com/ai-v5/keyless-web-mcp
```

Then copy `example.cordis.patch.yml` into your profile and replace
`<ABSOLUTE PATH TO keyless-web-provider.mjs>` with the path to this directory's
`keyless-web-provider.mjs`:

```bash
# merge the entries into the profile patch that the app reads
notepad "$env:USERPROFILE\.dsh\profiles\desktop\cordis.patch.yml"   # Windows
```

Selecting a provider is the `web` row's job; the plugin only registers the id:

```yaml
- id: web
  name: '@deepseek-ai/dsh-web'
  config:
    searchProvider: keyless
    fetchProvider: keyless
```

Restart the harness afterwards. A healthy load logs:

```
keyless-web-provider: registered web providers with id "keyless" (python auto)
keyless-web-provider: worker ready (pid 1234, python ...); provider "keyless" registered for search and fetch
```

## Config

| Key | Default | Meaning |
|---|---|---|
| `providerId` | `keyless` | registry id for **both** capabilities; must match `searchProvider` / `fetchProvider` |
| `python` | auto-detected | interpreter holding `keyless_web.worker`; auto-detection looks at `KEYLESS_WEB_PYTHON`, then the `uv tool` venv |
| `env` | `{}` | extra environment for the worker, e.g. `MCP_WEB_PROXY` |
| `maxResults` | `10` | hits per search when the request does not state a count (capped at 20) |
| `maxChars` | `20000` | text budget for one fetch |
| `includeLinks` | `false` | prepend the page's links to fetched text |
| `maxWorkerThreads` | `4` | requests the worker serves concurrently |
| `searchTimeoutMs` / `fetchTimeoutMs` | `60000` | per-request budget |
| `startTimeoutMs` | `20000` | cold interpreter + `trafilatura` import |

## Self-check

No harness and no network required — the script loads the plugin against a stub
context and a fake worker package, and proves the mapping, timeouts, crash
restart and abort handling:

```bash
node extras/dsh-provider/selftest.mjs
```
