/**
 * DSH web provider: keyless search and page reading.
 *
 * Registers one `WebSearchProvider` **and** one `WebFetchProvider` (default id
 * `keyless`) with `ctx.web`, so the harness's own `web_search` / `web_fetch`
 * tools run through this package instead of a keyed API. It adds no tools of its
 * own: the point is to replace a provider behind the existing seam, not to
 * introduce a parallel `mcp__*` namespace.
 *
 * ## How it works
 *
 * One long-lived Python worker (`python -m keyless_web.worker`) is spawned when
 * the plugin loads and serves every call over stdin/stdout JSON lines. Starting
 * an interpreter per call would cost more than the search itself, because
 * importing `trafilatura` dominates a cold process.
 *
 * The worker is restarted on the next call if it dies; a process that keeps
 * crashing, or a workspace whose interpreter cannot import the package, ends as
 * `available() === false` instead of throwing on every tool call. An
 * unavailable configured provider makes the harness answer
 * `WEB_PROVIDER_CONFIGURED_UNAVAILABLE`, which is the honest outcome.
 *
 * ## Config (all optional)
 *
 * ```yaml
 * - id: keyless-web
 *   name: 'D:\\path\\to\\keyless-web-provider.mjs'
 *   config:
 *     providerId: keyless          # registry key for BOTH capabilities
 *     python: 'C:\\...\\python.exe'  # interpreter holding keyless_web.worker
 *     env:                         # extra environment for the worker
 *       MCP_WEB_PROXY: http://127.0.0.1:7890
 *     maxChars: 20000              # web_fetch text budget
 *     includeLinks: false          # prepend page links to fetched text
 *     searchTimeoutMs: 60000
 *     fetchTimeoutMs: 60000
 *     startTimeoutMs: 20000        # cold interpreter + trafilatura import
 * ```
 *
 * Selecting it needs the `web` row's config, e.g.
 * `searchProvider: keyless` and `fetchProvider: keyless`.
 *
 * @module keyless-web-provider
 */

import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

/** Plugin name shown by the loader. */
const name = 'keyless-web-provider'

/** `ctx.web` must exist before this plugin registers anything. */
const inject = ['web']

/** Default registry id for both capabilities. */
const DEFAULT_PROVIDER_ID = 'keyless'

/** Result count when the request does not state one. */
const DEFAULT_MAX_RESULTS = 10

/** Hard cap, matching the worker's own `KEYLESS_MAX_RESULTS` policy. */
const MAX_RESULTS = 20

/** Fetched-text budget when the config does not state one. */
const DEFAULT_MAX_CHARS = 20000

const DEFAULT_SEARCH_TIMEOUT_MS = 60000
const DEFAULT_FETCH_TIMEOUT_MS = 60000
const DEFAULT_START_TIMEOUT_MS = 20000

/** Consecutive crash-restarts tolerated inside {@link RESTART_WINDOW_MS}. */
const RESTART_LIMIT = 3
const RESTART_WINDOW_MS = 60000

/**
 * How long a worker gets to answer `shutdown` before it is killed.
 *
 * A uv-managed venv's `python.exe` is a trampoline: the process Node holds is a
 * launcher whose child is the interpreter. Killing the launcher would orphan the
 * interpreter, so the protocol's own exit is tried first.
 */
const SHUTDOWN_GRACE_MS = 1500

/** A single protocol line may not exceed this; a runaway worker is killed. */
const MAX_LINE_CHARS = 16 * 1024 * 1024

/** Kept from the worker's stderr for the exit diagnostic. */
const STDERR_TAIL_LINES = 20

/**
 * Read one positive-integer option.
 *
 * @param {object} config raw plugin config.
 * @param {string} key option name.
 * @param {number} fallback default.
 * @returns {number} the configured value.
 */
function positiveInteger(config, key, fallback) {
  const value = config[key]
  if (value === undefined) return fallback
  if (!Number.isFinite(value) || value <= 0) {
    throw new TypeError(`${name}: config.${key} must be a positive number, got ${String(value)}`)
  }
  return Math.floor(value)
}

/**
 * Validate and default the plugin config once, so every call site reads plain
 * values. A bad value fails plugin load with a message naming the key, which is
 * far easier to act on than a failure inside the first tool call.
 *
 * @param {object | undefined} raw config from `cordis.patch.yml`.
 * @returns {object} the resolved settings.
 */
function normalizeConfig(raw) {
  const config = raw ?? {}
  if (typeof config !== 'object' || Array.isArray(config)) {
    throw new TypeError(`${name}: config must be an object`)
  }
  const providerId = config.providerId === undefined ? DEFAULT_PROVIDER_ID : config.providerId
  if (typeof providerId !== 'string' || providerId.length === 0) {
    throw new TypeError(`${name}: config.providerId must be a non-empty string`)
  }
  const python = config.python
  if (python !== undefined && (typeof python !== 'string' || python.length === 0)) {
    throw new TypeError(`${name}: config.python must be a non-empty path string`)
  }
  const env = config.env ?? {}
  if (typeof env !== 'object' || Array.isArray(env)) {
    throw new TypeError(`${name}: config.env must be an object of strings`)
  }
  return {
    providerId,
    python,
    env,
    maxResults: Math.min(positiveInteger(config, 'maxResults', DEFAULT_MAX_RESULTS), MAX_RESULTS),
    maxChars: positiveInteger(config, 'maxChars', DEFAULT_MAX_CHARS),
    includeLinks: config.includeLinks === true,
    maxWorkerThreads: positiveInteger(config, 'maxWorkerThreads', 4),
    searchTimeoutMs: positiveInteger(config, 'searchTimeoutMs', DEFAULT_SEARCH_TIMEOUT_MS),
    fetchTimeoutMs: positiveInteger(config, 'fetchTimeoutMs', DEFAULT_FETCH_TIMEOUT_MS),
    startTimeoutMs: positiveInteger(config, 'startTimeoutMs', DEFAULT_START_TIMEOUT_MS),
  }
}

/**
 * Candidate interpreters that carry `keyless_web.worker`.
 *
 * `uv tool install keyless-web-mcp` puts the package in a tool venv; that is the
 * interpreter a user who never opens a terminal would have. `KEYLESS_WEB_PYTHON`
 * and `config.python` override it for a checkout's own `.venv`.
 *
 * @returns {string[]} candidates, most specific first.
 */
function pythonCandidates() {
  const candidates = []
  const fromEnv = process.env.KEYLESS_WEB_PYTHON ?? process.env.DSH_KEYLESS_WEB_PYTHON
  if (fromEnv !== undefined && fromEnv.length > 0) candidates.push(fromEnv)
  const appData = process.env.APPDATA
  if (process.platform === 'win32' && appData !== undefined) {
    candidates.push(join(appData, 'uv', 'tools', 'keyless-web-mcp', 'Scripts', 'python.exe'))
  }
  candidates.push(join(homedir(), '.local', 'share', 'uv', 'tools', 'keyless-web-mcp', 'bin', 'python'))
  return candidates
}

/**
 * Pick the interpreter to spawn.
 *
 * @param {object} settings resolved plugin config.
 * @returns {string} an absolute candidate when one exists, else a PATH default.
 */
function resolvePython(settings) {
  if (settings.python !== undefined) return settings.python
  for (const candidate of pythonCandidates()) {
    if (existsSync(candidate)) return candidate
  }
  return process.platform === 'win32' ? 'python' : 'python3'
}

/**
 * Turn a worker error payload into the Error the seam will surface.
 *
 * The harness copies a tool failure's message into the model-facing result, so
 * the `KEYLESS_*` code and hint stay in the text instead of being flattened.
 *
 * @param {unknown} error decoded `error` member of a failed response.
 * @returns {Error} an error carrying the code and hint in its message.
 */
function protocolError(error) {
  const code = typeof error?.code === 'string' ? error.code : 'KEYLESS_UNKNOWN'
  const message = typeof error?.message === 'string' ? error.message : 'unknown failure'
  const hint = typeof error?.hint === 'string' && error.hint.length > 0 ? ` Hint: ${error.hint}` : ''
  return new Error(`keyless-web[${code}]: ${message}.${hint}`)
}

/**
 * Build the abort error the seam recognizes.
 *
 * `isAbortError` in the harness checks `instanceof DOMException` with
 * `name === 'AbortError'`; a plain Error would be reported as a provider fault.
 *
 * @returns {Error} an AbortError.
 */
function abortedError() {
  return new DOMException('the keyless web request was aborted', 'AbortError')
}

/** One long-lived worker process plus the pending-request ledger. */
class WorkerRuntime {
  #settings
  #logger
  #python
  #child
  #state = 'idle'
  #readyPromise
  #pending = new Map()
  #nextId = 1
  #buffer = ''
  #stderr = []
  #crashes = []
  #disposed = false

  /**
   * @param {object} settings resolved plugin config.
   * @param {object} logger the plugin context logger.
   */
  constructor(settings, logger) {
    this.#settings = settings
    this.#logger = logger
    this.#python = resolvePython(settings)
  }

  /**
   * Whether this provider could serve a call.
   *
   * Sync by contract, so it reports the durable facts: a spawn that failed, or a
   * crash loop, makes the provider unavailable; a worker that is merely not
   * started yet does not, because the next call starts it.
   *
   * @returns {boolean} whether calls should be attempted.
   */
  available() {
    return !this.#disposed && this.#state !== 'failed'
  }

  /** Start the worker now so the first tool call does not pay the cold start. */
  warmUp() {
    this.#ensureReady(undefined).then(
      () => {
        this.#logger.info(
          `${name}: worker ready (pid ${String(this.#child?.pid)}, python ${this.#python}); ` +
            `provider "${this.#settings.providerId}" registered for search and fetch`,
        )
      },
      (error) => {
        this.#logger.warn(`${name}: worker unavailable: ${error.message}`)
      },
    )
  }

  /**
   * Serve one search request.
   *
   * @param {{ query: string, maxResults?: number }} request the seam's request.
   * @param {AbortSignal} [signal] cancellation from the tool call.
   * @returns {Promise<{ sources: object[], truncated: boolean }>} the seam's result.
   */
  async search(request, signal) {
    const query = typeof request.query === 'string' ? request.query : ''
    if (query.trim().length === 0) throw new Error('keyless-web: a search needs a query')
    const maxResults = Math.min(
      Number.isInteger(request.maxResults) && request.maxResults > 0
        ? request.maxResults
        : this.#settings.maxResults,
      MAX_RESULTS,
    )
    const result = await this.#call('search', { query, maxResults }, this.#settings.searchTimeoutMs, signal)
    const hits = Array.isArray(result?.results) ? result.results : []
    const sources = hits.slice(0, maxResults).map((hit) => {
      const source = { url: String(hit.url ?? '') }
      if (hit.title) source.title = String(hit.title)
      if (hit.snippet) source.snippet = String(hit.snippet)
      if (hit.date) source.publishedAt = String(hit.date)
      return source
    })
    return { sources, truncated: result?.truncated === true || hits.length > maxResults }
  }

  /**
   * Serve one fetch request.
   *
   * @param {{ url: string }} request the seam's request.
   * @param {AbortSignal} [signal] cancellation from the tool call.
   * @returns {Promise<object>} the seam's `WebFetchResult`.
   */
  async fetch(request, signal) {
    const url = typeof request.url === 'string' ? request.url : ''
    if (url.trim().length === 0) throw new Error('keyless-web: a fetch needs a url')
    const result = await this.#call(
      'fetch',
      {
        url,
        maxChars: this.#settings.maxChars,
        includeLinks: this.#settings.includeLinks,
      },
      this.#settings.fetchTimeoutMs,
      signal,
    )
    const text = typeof result?.text === 'string' ? result.text : ''
    const statusCode = Number.isInteger(result?.statusCode) ? result.statusCode : 200
    return {
      url: typeof result?.url === 'string' && result.url.length > 0 ? result.url : url,
      statusCode,
      body: { kind: 'text', content: text },
      truncated: result?.truncated === true,
    }
  }

  /** Stop the worker; used by the plugin's dispose effect. */
  dispose() {
    this.#disposed = true
    this.#state = 'failed'
    this.#readyPromise = undefined
    this.#rejectAll(new Error('keyless-web: the plugin was disposed'))
    this.#terminate()
  }

  /**
   * Ask the worker to exit and kill it if it does not.
   *
   * @returns {void}
   */
  #terminate() {
    const child = this.#child
    this.#child = undefined
    if (child === undefined) return
    const timer = setTimeout(() => {
      child.kill()
    }, SHUTDOWN_GRACE_MS)
    timer.unref?.()
    child.once('exit', () => clearTimeout(timer))
    try {
      child.stdin.write(`${JSON.stringify({ id: this.#nextId++, method: 'shutdown', params: {} })}\n`)
    } catch {
      child.kill()
    }
  }

  // ------------------------------------------------------------------ process

  /**
   * Spawn the worker if it is not running.
   *
   * @returns {import('node:child_process').ChildProcessWithoutNullStreams} the child.
   */
  #spawn() {
    if (this.#child !== undefined) return this.#child
    const child = spawn(
      this.#python,
      ['-u', '-m', 'keyless_web.worker', '--max-workers', String(this.#settings.maxWorkerThreads)],
      {
        stdio: ['pipe', 'pipe', 'pipe'],
        windowsHide: true,
        env: {
          ...process.env,
          PYTHONIOENCODING: 'utf-8',
          PYTHONUTF8: '1',
          ...this.#settings.env,
        },
      },
    )
    this.#buffer = ''
    this.#stderr = []
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', (chunk) => this.#onChunk(chunk))
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', (chunk) => this.#onStderr(chunk))
    child.on('error', (error) => {
      this.#fail(`cannot start ${this.#python}: ${error.message}`)
    })
    child.on('exit', (code, signal) => this.#onExit(code, signal))
    // A closed stdin (or a dead worker) must not raise an unhandled 'error'.
    child.stdin.on('error', (error) => this.#logger.debug(`${name}: worker stdin: ${error.message}`))
    this.#child = child
    this.#state = 'starting'
    return child
  }

  /**
   * Resolve once the worker answered `ping`.
   *
   * @param {AbortSignal} [signal] cancellation.
   * @returns {Promise<void>} resolves when the worker is ready.
   */
  #ensureReady(signal) {
    if (this.#state === 'failed') {
      return Promise.reject(new Error(`keyless-web: the worker is unavailable (${this.#python})`))
    }
    if (this.#readyPromise === undefined) {
      const child = this.#spawn()
      this.#readyPromise = this.#exchange(child, 'ping', {}, this.#settings.startTimeoutMs, undefined)
        .then(
          (result) => {
            this.#state = 'ready'
            this.#logger.debug(`${name}: worker handshake ${JSON.stringify(result)}`)
          },
          (error) => {
            this.#readyPromise = undefined
            this.#fail(`worker handshake failed: ${error.message}`)
            throw error
          },
        )
    }
    if (signal === undefined) return this.#readyPromise
    return Promise.race([
      this.#readyPromise,
      new Promise((_resolve, reject) => {
        if (signal.aborted) {
          reject(abortedError())
          return
        }
        signal.addEventListener('abort', () => reject(abortedError()), { once: true })
      }),
    ])
  }

  /**
   * Send one request and resolve its answer.
   *
   * @param {string} method `search` or `fetch`.
   * @param {object} params method parameters.
   * @param {number} timeoutMs budget for this request.
   * @param {AbortSignal} [signal] cancellation.
   * @returns {Promise<any>} the worker's `result` member.
   */
  async #call(method, params, timeoutMs, signal) {
    await this.#ensureReady(signal)
    const child = this.#child
    if (child === undefined) throw new Error('keyless-web: the worker is not running')
    return await this.#exchange(child, method, params, timeoutMs, signal)
  }

  /**
   * Write one request line and settle on its response, timeout or abort.
   *
   * @param {import('node:child_process').ChildProcessWithoutNullStreams} child the worker.
   * @param {string} method method name.
   * @param {object} params method parameters.
   * @param {number} timeoutMs budget for this request.
   * @param {AbortSignal} [signal] cancellation.
   * @returns {Promise<any>} the response's `result` member.
   */
  #exchange(child, method, params, timeoutMs, signal) {
    const id = this.#nextId++
    return new Promise((resolve, reject) => {
      if (signal?.aborted === true) {
        reject(abortedError())
        return
      }
      let onAbort
      const cleanup = () => {
        clearTimeout(timer)
        if (signal !== undefined && onAbort !== undefined) signal.removeEventListener('abort', onAbort)
      }
      const timer = setTimeout(() => {
        this.#pending.delete(id)
        cleanup()
        reject(
          new Error(
            `keyless-web: ${method} did not answer within ${timeoutMs}ms ` +
              `(worker pid ${String(child.pid)})`,
          ),
        )
      }, timeoutMs)
      timer.unref?.()
      if (signal !== undefined) {
        onAbort = () => {
          this.#pending.delete(id)
          cleanup()
          reject(abortedError())
        }
        signal.addEventListener('abort', onAbort, { once: true })
      }
      this.#pending.set(id, {
        method,
        settle(ok, value) {
          cleanup()
          if (ok) resolve(value)
          else reject(value)
        },
      })
      try {
        child.stdin.write(`${JSON.stringify({ id, method, params })}\n`)
      } catch (error) {
        this.#pending.delete(id)
        cleanup()
        reject(error)
      }
    })
  }

  // ------------------------------------------------------------------ inbound

  /**
   * Consume stdout, answering every complete line.
   *
   * @param {string} chunk decoded stdout data.
   */
  #onChunk(chunk) {
    this.#buffer += chunk
    let index = this.#buffer.indexOf('\n')
    while (index >= 0) {
      const line = this.#buffer.slice(0, index).trim()
      this.#buffer = this.#buffer.slice(index + 1)
      if (line.length > 0) this.#onLine(line)
      index = this.#buffer.indexOf('\n')
    }
    if (this.#buffer.length > MAX_LINE_CHARS) {
      this.#buffer = ''
      this.#fail('a worker response line exceeded the protocol limit')
    }
  }

  /**
   * Settle the pending request a response belongs to.
   *
   * @param {string} line one protocol line.
   */
  #onLine(line) {
    let payload
    try {
      payload = JSON.parse(line)
    } catch {
      this.#logger.warn(`${name}: ignoring an unparseable worker line`)
      return
    }
    const entry = this.#pending.get(payload.id)
    if (entry === undefined) {
      // A response that arrived after its request timed out or was aborted.
      this.#logger.debug(`${name}: dropping a late ${String(payload.id)} response`)
      return
    }
    this.#pending.delete(payload.id)
    if (payload.ok === true) entry.settle(true, payload.result)
    else entry.settle(false, protocolError(payload.error))
  }

  /**
   * Keep the worker's stderr for diagnostics; the protocol owns stdout only.
   *
   * @param {string} chunk decoded stderr data.
   */
  #onStderr(chunk) {
    for (const line of chunk.split(/\r?\n/)) {
      const statement = line.trim()
      if (statement.length === 0) continue
      this.#stderr.push(statement)
      if (this.#stderr.length > STDERR_TAIL_LINES) this.#stderr.shift()
      this.#logger.debug(`${name}: worker: ${statement}`)
    }
  }

  /**
   * Handle the worker going away: fail in-flight calls and decide on a restart.
   *
   * @param {number | null} code exit code, when the process exited.
   * @param {string | null} signal termination signal, when it was killed.
   */
  #onExit(code, signal) {
    const detail = signal === null ? `code ${String(code)}` : `signal ${signal}`
    const tail = this.#stderr.length > 0 ? `: ${this.#stderr.at(-1)}` : ''
    this.#child = undefined
    this.#readyPromise = undefined
    this.#buffer = ''
    if (this.#pending.size > 0) {
      this.#rejectAll(new Error(`keyless-web: the worker exited (${detail})${tail}`))
    }
    if (this.#disposed || this.#state === 'failed') return
    const now = Date.now()
    this.#crashes = this.#crashes.filter((at) => now - at < RESTART_WINDOW_MS)
    this.#crashes.push(now)
    if (this.#crashes.length > RESTART_LIMIT) {
      this.#fail(
        `the worker exited ${this.#crashes.length} times in ${RESTART_WINDOW_MS / 1000}s (${detail})${tail}`,
      )
      return
    }
    this.#state = 'idle'
    this.#logger.warn(`${name}: worker exited (${detail})${tail}; it restarts on the next call`)
  }

  /**
   * Mark the provider unavailable; every later call fails fast.
   *
   * @param {string} reason why the worker is being given up on.
   */
  #fail(reason) {
    const alreadyFailed = this.#state === 'failed'
    this.#state = 'failed'
    this.#readyPromise = undefined
    this.#rejectAll(new Error(`keyless-web: ${reason}`))
    this.#terminate()
    if (alreadyFailed) return
    const tail = this.#stderr.length > 0 ? ` (last stderr: ${String(this.#stderr.at(-1))})` : ''
    this.#logger.error(`${name}: ${reason}${tail}`)
  }

  /**
   * Reject every pending request with one error.
   *
   * @param {Error} error the failure to propagate.
   */
  #rejectAll(error) {
    const entries = [...this.#pending.values()]
    this.#pending.clear()
    for (const entry of entries) entry.settle(false, error)
  }
}

/**
 * Register the keyless search and fetch providers with `ctx.web`.
 *
 * @param {object} ctx plugin context; `web` is injected.
 * @param {object | undefined} config raw plugin config.
 */
function apply(ctx, config) {
  const settings = normalizeConfig(config)
  const runtime = new WorkerRuntime(settings, ctx.logger)
  ctx.effect(() => () => runtime.dispose())

  ctx.web.registerSearchProvider({
    id: settings.providerId,
    available: () => runtime.available(),
    search: (request, signal) => runtime.search(request, signal),
  })
  ctx.web.registerFetchProvider({
    id: settings.providerId,
    available: () => runtime.available(),
    fetch: (request, signal) => runtime.fetch(request, signal),
  })
  ctx.logger.info(
    `${name}: registered web providers with id "${settings.providerId}" ` +
      `(python ${String(settings.python ?? 'auto')})`,
  )
  runtime.warmUp()
}

export { apply, inject, name, normalizeConfig, resolvePython }
