# Contributing

Thanks for taking a look. This project is small on purpose: two tools, four engines, no
configuration file.

## Set up

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows; .venv/bin/activate elsewhere
pip install -e ".[dev]"
```

## Checks before sending a change

```bash
ruff check .
ruff format --check .
mypy src
pytest -q
```

CI runs the same four commands on Linux and Windows with Python 3.10 and 3.12.

## Architecture rules

- **Engines stay thin.** A module under `src/keyless_web/engines/` exposes
  `search(client, query, max_results) -> list[SearchResult]` and nothing else. It receives an
  `httpx.Client`; it never constructs one, never retries, never reads the environment.
- **Network policy has one home.** Proxy fallback, retry rules and rate limiting live in
  `src/keyless_web/net/transport.py`. Do not re-implement them per engine.
- **`server.py` is a shell.** Tool declarations and argument validation only; behaviour belongs in
  `engines/` or `fetch/`.
- **Errors are part of the API.** Raise a `KeylessWebError` subclass from `errors.py` with a
  stable `KEYLESS_*` code and an actionable hint. Never surface a bare exception to the model.

## Adding an engine

1. Add `src/keyless_web/engines/<name>.py` with `NAME` and `search(client, query, max_results)`.
2. Register it in `ENGINES` in `src/keyless_web/engines/__init__.py` and add the name to
   `ENGINE_NAMES` in `src/keyless_web/config.py`.
3. Add it to `DEFAULT_ENGINES` only if it is stable and polite by default.
4. Unit-test it with `httpx.MockTransport` and a saved fixture — tests must never hit the network.
5. Document it in the engine table in `README.md` and `README.zh-CN.md`.

## Tests must not touch the network

Everything under `tests/` runs offline. Inject a transport
(`httpx.MockTransport`) or call the pure extraction helpers directly. A test that reaches the
internet is a bug: it makes CI flaky and hammers third-party sites.

## Commit style

Short imperative subject, one logical change per commit. Reference the affected module when it
helps (`engines: handle DDG redirect links`). Keep the diff focused; unrelated reformatting makes
review harder.
