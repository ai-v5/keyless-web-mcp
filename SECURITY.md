# Security policy

## What this project is

`keyless-web-mcp` is a local stdio MCP server. It has no authentication, no listening socket and no
server-side state: the MCP client starts it as a child process and talks to it over stdin/stdout.

## Reporting a vulnerability

Please report suspected vulnerabilities privately, through GitHub's **"Report a vulnerability"**
(Security → Advisories) on this repository rather than in a public issue. Include the version, the
tool call that triggers the problem, and what you expected.

Please do not open a public issue for anything that could be exploited before a fix ships.

## Threat model notes

Relevant by design:

- **Fetching arbitrary URLs.** `web_fetch` accepts any absolute `http(s)` URL, including
  `http://127.0.0.1`, `http://169.254.169.254` and other internal addresses. There is deliberately
  no allowlist: the tool exists to read the web, and a local agent already has network access. If
  you expose this server to untrusted input, put the network policy around the process (egress
  rules, a proxy), not around the URL string.
- **Prompt injection.** Fetched pages are untrusted content and may contain instructions aimed at
  the model. Results are passed through verbatim; treat them as data. Do not let a page change what
  the agent is allowed to do.
- **TLS.** Certificate verification is always on; `truststore` loads the OS trust store so
  interception proxies work. Nothing disables verification.
- **Secrets.** The server needs no credential and reads none. It does not read your files, and it
  caches responses in memory only (no disk writes).

Out of scope: upstream sites changing their markup, being rate-limited or blocked, and the accuracy
of third-party content.
