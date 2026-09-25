# ADR 0002 — Minimal dependency set

- **Status:** Accepted
- **Date:** 2026-09-25

## Context

ExcelPilot is a security-sensitive tool that parses untrusted files and calls paid
external services. Every dependency is additional attack surface, additional supply-chain
risk, and a constraint on who can run the project.

The user's own global instructions state: *"Before adding a dependency: check whether an
existing dependency already provides the capability; prefer maintained and well-supported
packages; avoid unnecessary dependency proliferation."*

## Decision

**Five required dependencies.** Everything else is optional or standard library.

| Package | Why it is required |
|---|---|
| `openpyxl` | The workbook engine. No stdlib equivalent. (ADR-0001) |
| `pydantic` | Typed contracts and validation of untrusted model/JEV output. Hand-rolled validators would be a large, bug-prone surface — exactly what must not guard security boundaries. |
| `typer` | CLI. Small, typed, builds on `click`. |
| `rich` | CLI output. Required by `typer`'s formatting path. |
| `defusedxml` | Hardened XML parsing for untrusted workbook parts. Hardened XML is a security control, not a convenience. |

Optional extras:

- `dashboard`: `fastapi`, `uvicorn`, `jinja2` — only for the final dashboard phase.
- `recalc`: `pycel` — only if Phase 6 proves it works.
- `dev`: `pytest`, `pytest-cov`, `ruff`, `mypy`, `hypothesis`.

## Explicitly rejected

| Rejected | Reason |
|---|---|
| `httpx` / `requests` / `aiohttp` | JEV's own `jev.py` uses stdlib `urllib` for a reason: zero dependencies, no redirect-following surprises, and complete control over timeouts. ExcelPilot makes a handful of HTTPS calls. `urllib` is sufficient and removes a dependency from the path that handles API keys. |
| `pandas` / `numpy` | Only needed for tabular math that ExcelPilot performs over `openpyxl` cells directly. Adding a DataFrame layer would obscure exactly the cell-level provenance the diff engine depends on. |
| `sqlalchemy` / any ORM | Audit storage is append-only JSONL. See ADR-0007. |
| `jinja2` as a core dep | Only the dashboard needs it. |
| `python-dotenv` | Deliberate: credentials come from the process environment only, so a `.env` in the working directory cannot silently redirect traffic. |

## Consequences

**Positive**
- Clone → `uv sync` → run. No compiler, no external binary.
- Small, auditable supply chain: 5 packages for a security-sensitive tool.
- The dependency that handles API keys has no dependencies of its own.

**Negative**
- Some conveniences are hand-written: the LLM adapter's response parsing, the CLI's
  JSON output, argument validation. Each is small and tested.
- No built-in retry/backoff library. Implemented explicitly in
  `app/net/http.py` with the "no automatic retry on provider errors" rule that JEV
  itself documents.
