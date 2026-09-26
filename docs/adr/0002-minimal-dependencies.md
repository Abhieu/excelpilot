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

**`defusedxml` is declared directly, not left to transitive resolution.** The
audit for this ADR re-verified the mechanism rather than trusting the comment:
`defusedxml` is imported by `openpyxl.xml.functions`, so `grep -r "defusedxml"
app/` finds nothing and makes the dependency look unused. Removing it on that
evidence would have silently downgraded every OOXML parse from hardened to
stdlib `xml.etree.ElementTree`, with entity expansion enabled.

Verified: during one `inspect_workbook` call on a small workbook, `defusedxml`'s
`fromstring` handled 55 of 55 XML parses.

The protection is conditional on openpyxl's `OPENPYXL_DEFUSEDXML` environment
variable, which defaults to `"True"` but can be set to `"False"` to disable it.
ExcelPilot cannot detect that, and it is documented in
[`docs/security.md`](../security.md) as an operational caveat.

Optional extras:

- `recalc`: `formulas` — formula recalculation in verification. Measured working
  in Phase 6 (`pycel` was evaluated and rejected for the wrong API surface).
  Pulls in scipy, which is why it is not a core dependency. It is also installed
  by `dev` so the recalculation path is exercised in CI.
- `dev`: `pytest`, `ruff`, `mypy`, plus `formulas` (so CI exercises the
  recalculation path).

**Amended twice.**

A `dashboard` extra (`fastapi`, `uvicorn`, `jinja2`) was originally declared for
a planned web UI. The UI was deliberately not built — it would have to
re-implement the approval and policy gate to be trustworthy, and a UI that
bypasses either is worse than no UI. The extra was removed rather than left
declaring three web packages for a module that does not exist.

`pytest-cov` and `hypothesis` were also removed. No test imports either, and
there is no `--cov` anywhere in the Makefile or CI, so they were declared
tooling for a practice the repository does not have. `hypothesis` in particular
is the library you declare *before* writing property-based tests; declaring it
and writing none is a claim about testing that is not true. Reinstating either is
a one-line change when the corresponding practice starts.

| Rejected | Reason |
|---|---|
| `fastapi` / `uvicorn` / `jinja2` | No dashboard was built. Declaring the extra would be dependency surface for a feature the repository does not have. |
| `pytest-cov` | No coverage reporting is configured in the Makefile or CI. |
| `hypothesis` | No property-based test exists. Declaring it implies a practice the repository does not follow. |

## Explicitly rejected

| Rejected | Reason |
|---|---|
| `httpx` / `requests` / `aiohttp` | JEV's own `jev.py` uses stdlib `urllib` for a reason: zero dependencies, no redirect-following surprises, and complete control over timeouts. ExcelPilot makes a handful of HTTPS calls. `urllib` is sufficient and removes a dependency from the path that handles API keys. |
| `pandas` / `numpy` | Only needed for tabular math that ExcelPilot performs over `openpyxl` cells directly. Adding a DataFrame layer would obscure exactly the cell-level provenance the diff engine depends on. |
| `sqlalchemy` / any ORM | Audit storage is append-only JSONL. See ADR-0007. |
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
