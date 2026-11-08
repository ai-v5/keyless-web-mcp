/**
 * Development self-check for `keyless-web-provider.mjs` — no harness, no network.
 *
 * `node extras/dsh-provider/selftest.mjs`
 *
 * It loads the plugin against a stub `ctx` and a **fake** worker package (a
 * throwaway `keyless_web.worker` on `PYTHONPATH`), so it can prove the pieces
 * that unit tests of the real worker cannot reach: response mapping, error
 * mapping, the per-request timeout, the late-response drop, and restart after a
 * worker crash. Point `KEYLESS_WEB_PYTHON` at an interpreter if the repo's
 * `.venv` is not where it is expected.
 */

import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, realpathSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { apply, normalizeConfig, resolvePython } from './keyless-web-provider.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = join(HERE, '..', '..')

const FAKE_WORKER = `
import json, os, sys, threading, time

LOCK = threading.Lock()

def respond(payload):
    with LOCK:
        sys.stdout.write(json.dumps(payload) + "\\n")
        sys.stdout.flush()

def handle(line):
    request = json.loads(line)
    rid = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    if method == "ping":
        respond({"id": rid, "ok": True,
                 "result": {"name": "fake-worker", "version": "0.0.0", "protocol": 1,
                            "engines": ["duckduckgo"]}})
    elif method == "search":
        query = params.get("query", "")
        if query == "crash":
            os._exit(3)
        if query == "slow":
            time.sleep(10)
        if query == "error":
            respond({"id": rid, "ok": False,
                     "error": {"code": "KEYLESS_NETWORK", "message": "all backends failed",
                               "hint": "set MCP_WEB_PROXY"}})
            return
        respond({"id": rid, "ok": True,
                 "result": {"query": query, "truncated": True,
                            "results": [{"title": "First", "url": "https://example.com/1",
                                         "snippet": "snip", "engine": "duckduckgo",
                                         "date": "2026-01-02"},
                                        {"url": "https://example.com/2"}]}})
    elif method == "fetch":
        respond({"id": rid, "ok": True,
                 "result": {"url": params.get("url"), "statusCode": 404,
                            "text": "no such page", "truncated": True}})
    else:
        respond({"id": rid, "ok": False,
                 "error": {"code": "KEYLESS_BAD_REQUEST", "message": "unknown", "hint": ""}})

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    threading.Thread(target=handle, args=(line,), daemon=True).start()
`

/**
 * Find an interpreter: the repo venv first, then whatever the plugin would pick.
 *
 * @returns {string} absolute python path.
 */
function findPython() {
  const fromEnv = process.env.KEYLESS_WEB_PYTHON
  if (fromEnv !== undefined && existsSync(fromEnv)) return fromEnv
  for (const relative of ['Scripts/python.exe', 'bin/python']) {
    const candidate = join(REPO_ROOT, '.venv', relative)
    if (existsSync(candidate)) return candidate
  }
  const resolved = resolvePython({ providerId: 'keyless', env: {} })
  if (resolved === 'python' || resolved === 'python3') {
    return realpathSync(execFileSync(process.platform === 'win32' ? 'where' : 'which', [resolved], {
      encoding: 'utf8',
    }).split(/\r?\n/)[0].trim())
  }
  return resolved
}

/** A stub of the parts of the harness plugin context the provider touches. */
function makeContext() {
  const logs = []
  const searches = new Map()
  const fetches = new Map()
  const disposers = []
  const record = (level) => (message) => logs.push(`${level}: ${message}`)
  return {
    logs,
    searches,
    fetches,
    dispose() {
      for (const disposer of disposers.reverse()) disposer()
    },
    ctx: {
      logger: { debug: record('debug'), info: record('info'), warn: record('warn'), error: record('error') },
      effect(factory) {
        disposers.push(factory())
      },
      web: {
        registerSearchProvider(provider) {
          searches.set(provider.id, provider)
          const dispose = () => searches.delete(provider.id)
          disposers.push(dispose)
          return dispose
        },
        registerFetchProvider(provider) {
          fetches.set(provider.id, provider)
          const dispose = () => fetches.delete(provider.id)
          disposers.push(dispose)
          return dispose
        },
      },
    },
  }
}

/**
 * Run one labelled check.
 *
 * @param {string} label what is being proven.
 * @param {() => Promise<void>} body the check.
 */
async function check(label, body) {
  await body()
  process.stdout.write(`ok   ${label}\n`)
}

const python = findPython()
const fakeRoot = mkdtempSync(join(tmpdir(), 'keyless-fake-'))
mkdirSync(join(fakeRoot, 'keyless_web'), { recursive: true })
writeFileSync(join(fakeRoot, 'keyless_web', '__init__.py'), '')
writeFileSync(join(fakeRoot, 'keyless_web', 'worker.py'), FAKE_WORKER)

const harness = makeContext()
apply(harness.ctx, {
  python,
  env: { PYTHONPATH: fakeRoot },
  searchTimeoutMs: 1500,
  fetchTimeoutMs: 5000,
  startTimeoutMs: 20000,
  maxChars: 1234,
})

const search = harness.searches.get('keyless')
const fetched = harness.fetches.get('keyless')

await check('registers one search and one fetch provider', async () => {
  assert.ok(search, 'no search provider registered')
  assert.ok(fetched, 'no fetch provider registered')
  assert.equal(search.available(), true)
})

await check('maps hits onto WebSearchResult.sources', async () => {
  const result = await search.search({ query: 'hello', maxResults: 5 })
  assert.deepEqual(result.sources, [
    { url: 'https://example.com/1', title: 'First', snippet: 'snip', publishedAt: '2026-01-02' },
    { url: 'https://example.com/2' },
  ])
  assert.equal(result.truncated, true)
})

await check('maps a response onto WebFetchResult', async () => {
  const result = await fetched.fetch({ url: 'https://example.com/missing' })
  assert.deepEqual(result, {
    url: 'https://example.com/missing',
    statusCode: 404,
    body: { kind: 'text', content: 'no such page' },
    truncated: true,
  })
})

await check('surfaces a KEYLESS_* failure with its hint', async () => {
  await assert.rejects(search.search({ query: 'error' }), (error) => {
    assert.match(error.message, /keyless-web\[KEYLESS_NETWORK\]/)
    assert.match(error.message, /set MCP_WEB_PROXY/)
    return true
  })
})

await check('times out a wedged request and drops its late response', async () => {
  await assert.rejects(search.search({ query: 'slow' }), /did not answer within 1500ms/)
  // The worker is busy for ten more seconds; the next call must still answer.
  const result = await search.search({ query: 'after-timeout' })
  assert.equal(result.sources.length, 2)
})

await check('restarts the worker after it crashes', async () => {
  await assert.rejects(search.search({ query: 'crash' }), /worker exited/)
  assert.equal(search.available(), true)
  const result = await search.search({ query: 'after-crash' })
  assert.equal(result.sources[0].url, 'https://example.com/1')
})

await check('rejects with an AbortError the seam recognizes', async () => {
  const controller = new AbortController()
  const pending = fetched.fetch({ url: 'https://example.com/slow' }, controller.signal)
  controller.abort()
  await assert.rejects(pending, (error) => {
    assert.equal(error.name, 'AbortError')
    return true
  })
})

await check('refuses an empty query instead of calling the worker', async () => {
  await assert.rejects(search.search({ query: '  ' }), /needs a query/)
})

await check('disposes the worker and reports itself unavailable', async () => {
  harness.dispose()
  assert.equal(harness.searches.size, 0)
  assert.equal(harness.fetches.size, 0)
  assert.equal(search.available(), false)
})

await check('rejects a bad config with the offending key', async () => {
  assert.throws(() => normalizeConfig({ searchTimeoutMs: -1 }), /config\.searchTimeoutMs/)
  assert.throws(() => normalizeConfig({ providerId: '' }), /config\.providerId/)
  assert.equal(normalizeConfig(undefined).providerId, 'keyless')
})

await check('logged the registration and the handshake', async () => {
  const joined = harness.logs.join('\n')
  assert.match(joined, /registered web providers with id "keyless"/)
  assert.match(joined, /worker ready \(pid \d+/)
})

process.stdout.write(`${harness.logs.filter((line) => !line.startsWith('debug:')).join('\n')}\n`)
process.stdout.write('all checks passed\n')
