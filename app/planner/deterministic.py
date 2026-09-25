"""Deterministic natural-language → ExecutionPlan compiler.

## Why this exists

No LLM credential exists in this environment, and a system whose headline
capability is unrunnable without a purchased key is neither demonstrable nor
testable in CI. For the operation set ExcelPilot supports, the mapping from
request to plan is genuinely finite, so a rule-based compiler is correct,
deterministic, free, and auditable (ADR-0003).

## The honesty rule

When a request is under-specified, this planner does **not** guess. It returns
``InterpretationStatus.REQUIRES_USER_INPUT`` listing exactly which facts are
missing, and policy turns that into a hard ``DENY`` (``no_guessing``). A
plausible workbook nobody asked for is worse than a refusal.

Grounding is real: every sheet and column a plan references must exist in the
``WorkbookInspection`` the planner was given. A request naming a sheet that is not
in the workbook produces a missing-information signal, not a hallucinated
operation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from app.contracts.base import UntrustedText
from app.contracts.enums import InterpretationVerdict
from app.contracts.operations import (
    ApplyValidation,
    CreateSummary,
    FilterCondition,
    FilterRows,
    Measure,
    NormalizeRules,
    NormalizeValues,
    ReadRange,
    RemoveDuplicates,
    SortRange,
    SummarySpec,
    Target,
    WorkbookOperation,
)
from app.contracts.pipeline import ExecutionPlan, TaskUnderstanding
from app.contracts.workbook import SheetMetadata, WorkbookInspection


class Planner(Protocol):
    """Turns a task plus a workbook inspection into a validated plan."""

    def plan(self, task: UntrustedText, inspection: WorkbookInspection) -> ExecutionPlan: ...


@dataclass(frozen=True, slots=True)
class Intent:
    """One recognised intent from the request."""

    name: str
    sheet: str | None = None
    columns: tuple[str, ...] = ()
    sort_by: tuple[str, ...] = ()
    descending: bool = False
    limit: int | None = None
    phrase: str = ""
    """The full request text, so an intent builder can look at context around
    its own keywords. Destructive operations need this to avoid guessing."""


@dataclass(slots=True)
class _SheetCandidate:
    """A sheet the request might mean, with how it was matched."""

    sheet: SheetMetadata
    matched_explicitly: bool
    score: int


#: Column-name synonyms. Real workbooks label the same concept many ways, and a
#: planner that only matches exact strings would be useless on real data.
COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "customer": ("customer", "customers", "client", "clientname", "account", "accountname", "name"),
    "date": ("date", "orderdate", "invoicedate", "transactiondate", "postingdate", "day"),
    "amount": ("amount", "total", "value", "net", "sum", "price", "revenue", "sales"),
    "quantity": ("quantity", "qty", "units", "count", "volume"),
    "region": ("region", "area", "territory", "zone", "location", "market"),
    "product": ("product", "item", "sku", "productname", "article"),
    "status": ("status", "state", "stage", "paymentstatus"),
    "invoice": ("invoice", "invoiceid", "invoiceno", "invoice_number", "bill", "docno", "ref"),
    "id": ("id", "identifier", "key", "pk", "rowid"),
    "email": ("email", "emailaddress", "mail", "contactemail"),
    "notes": ("notes", "note", "comment", "comments", "remarks", "description"),
}

#: Intent patterns. Each maps to a recogniser; order matters only for reporting,
#: not for correctness, since all matching intents are collected.
_INTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("remove_duplicates", re.compile(r"\b(remove|delete|drop|strip)\b[^.]*\bduplicat", re.I)),
    ("remove_duplicates", re.compile(r"\bdedup|\bde-?dup", re.I)),
    ("dedupe_keyed", re.compile(r"duplicat\w*[^.]*\bby\b", re.I)),
    ("normalize", re.compile(r"\b(normalis|normaliz|clean|trim|standardis|standardiz|tidy)", re.I)),
    ("sort", re.compile(r"\bsort\b|\border\b[^.]*\bby\b", re.I)),
    ("filter", re.compile(r"\bfilter\b|\bonly (?:keep|show|include)\b|\bwhere\b", re.I)),
    (
        "summarize",
        re.compile(r"\bsummar\w+|\bpivot\b|\bbreakdown\b|\baggregat\w+|\btotal by\b", re.I),
    ),
    ("validate", re.compile(r"\bvalidat\w+|\bcheck\b[^.]*\b(invalid|missing|required)\b", re.I)),
    ("read", re.compile(r"^\s*(read|show|list|display|inspect|report)\b", re.I)),
)

#: Injection-ish or out-of-scope requests are refused explicitly rather than
#: guessed at. A refusal with a stated reason is far better than a plan that
#: quietly does the wrong thing.
_UNSUPPORTED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("delete_sheet", re.compile(r"\b(delete|remove|drop)\b[^.]*\bsheets?\b", re.I)),
    ("delete_column", re.compile(r"\b(delete|remove|drop)\b[^.]*\bcolumns?\b", re.I)),
    (
        "run_code",
        re.compile(r"\b(run|execute|eval)\b[^.]*\b(python|script|code|macro|sql)\b", re.I),
    ),
    ("send_email", re.compile(r"\b(email|e-?mail|send)\b[^.]*\bto\b|\bsend\b[^.]*\bemail\b", re.I)),
    ("merge_workbooks", re.compile(r"\bmerge\b[^.]*\b(workbooks?|files?)\b", re.I)),
)

_SHEET_QUOTED = re.compile(r"['\"`]([^'\"`]{1,64})['\"`]")
_SHEET_AFTER_PREPOSITION = re.compile(
    r"\b(?:sheet|tab|worksheet)\s+(?:called|named|titled)?\s*['\"\`]?([A-Za-z0-9 _.\-]{1,64})['\"`]?",
    re.I,
)
_BY_COLUMN = re.compile(r"\bby\s+['\"`]?([A-Za-z][A-Za-z0-9_.\-]{0,38})['\"`]?", re.I)
#: "the Customer column", "Customer field" - the other common way a request names
#: a column, alongside "by <column>".
#:
#: The captured name is deliberately a **single token**. An earlier version allowed
#: spaces, which let the match start at the beginning of the sentence: given
#: "normalise the Customer column, remove duplicate invoices", the regex captured
#: "normalise the Customer" rather than "Customer", because a regex engine takes
#: the earliest viable start position. Multi-word headers remain reachable through
#: the synonym table and ``find_header``.
_COLUMN_MENTION = re.compile(
    r"(?:\b(?:the|this|that|each|every|its|their|same|one|a|an)\s+)?"
    r"['\"`]?([A-Za-z][A-Za-z0-9_.\-]{0,38})['\"`]?\s+"
    r"(?:column|field)\b",
    re.I,
)
_LIMIT = re.compile(r"\b(?:top|first|limit)\s+(\d{1,7})\b", re.I)

#: Concept words a request can use in place of a column name, mapped to the
#: column synonyms that satisfy them.
_CONCEPT_TOKENS: dict[str, tuple[str, ...]] = {
    "invoice": COLUMN_SYNONYMS["invoice"],
    "invoices": COLUMN_SYNONYMS["invoice"],
    "customer": COLUMN_SYNONYMS["customer"],
    "customers": COLUMN_SYNONYMS["customer"],
    "client": COLUMN_SYNONYMS["customer"],
    "clients": COLUMN_SYNONYMS["customer"],
    "region": COLUMN_SYNONYMS["region"],
    "regions": COLUMN_SYNONYMS["region"],
    "product": COLUMN_SYNONYMS["product"],
    "products": COLUMN_SYNONYMS["product"],
    "date": COLUMN_SYNONYMS["date"],
    "dates": COLUMN_SYNONYMS["date"],
    "amount": COLUMN_SYNONYMS["amount"],
    "amounts": COLUMN_SYNONYMS["amount"],
    "quantity": COLUMN_SYNONYMS["quantity"],
    "status": COLUMN_SYNONYMS["status"],
    "id": COLUMN_SYNONYMS["id"],
    "identifier": COLUMN_SYNONYMS["id"],
    "email": COLUMN_SYNONYMS["email"],
}


def _first_unsupported(lowered: str) -> tuple[str, str] | None:
    """The first unsupported request pattern, with a reason an operator can act on."""
    for name, pattern in _UNSUPPORTED_PATTERNS:
        if pattern.search(lowered):
            return (
                name,
                f"the request asks for something ExcelPilot does not support ({name}); "
                f"supported operations are normalise, remove duplicates, sort, filter, "
                f"summarise, validate, and read",
            )
    return None


#: Shown to the operator when a request is refused, keyed by reason.
UNSUPPORTED_GUIDANCE: dict[str, str] = {
    "delete_sheet": "ExcelPilot never deletes a worksheet. Create a summary sheet instead.",
    "delete_column": (
        "ExcelPilot does not delete columns. Filter to the columns you need, or write them "
        "to a new sheet."
    ),
    "run_code": (
        "ExcelPilot does not execute generated code. Describe the change in terms of the "
        "supported operations."
    ),
    "send_email": "ExcelPilot does not send email. It writes files and reports.",
    "merge_workbooks": (
        "ExcelPilot operates on one workbook at a time. Use the compare operation to "
        "reconcile two files."
    ),
    "no_supported_operation": (
        "ExcelPilot supports: normalise, remove duplicates, sort, filter, summarise, "
        "validate, and read."
    ),
}


class DeterministicPlanner:
    """Rule-based planner. No network, no credentials, fully deterministic."""

    source = "deterministic"

    def plan(self, task: UntrustedText, inspection: WorkbookInspection) -> ExecutionPlan:
        """Compile a request into a plan, or explain precisely why it cannot be."""
        text = task.text
        lowered = text.lower()

        unsupported = _first_unsupported(lowered)
        if unsupported:
            return self._refusal(task, inspection, unsupported[0], [unsupported[1]])

        sheets = _candidate_sheets(text, inspection)
        intents = _detect_intents(text)

        if not intents:
            return self._refusal(
                task,
                inspection,
                "no_supported_operation",
                [
                    "the request did not match any operation ExcelPilot supports "
                    "(normalise, remove duplicates, sort, filter, summarise, validate, read)"
                ],
            )

        if not sheets:
            return self._refusal(
                task,
                inspection,
                "no_target_sheet",
                [
                    "no worksheet in the workbook could be identified as the target; "
                    f"available sheets: {', '.join(inspection.sheet_names)}"
                ],
            )

        operations: list[WorkbookOperation] = []
        missing: list[str] = []
        notes: list[str] = []
        # Every intent is built against the best-ranked sheet. A plan that spans
        # sheets is expressed as several sheets in `referenced_sheets`; the v1
        # grammar targets one sheet per request, which is the common case and the
        # one an operator can verify at a glance.
        target_sheet = sheets[0].sheet

        for intent in intents:
            built, intent_missing, intent_notes = self._build(intent, target_sheet, inspection)
            operations.extend(built)
            missing.extend(intent_missing)
            notes.extend(intent_notes)

        if not operations:
            return self._refusal(
                task, inspection, "no_operations_built", missing or ["no operations were produced"]
            )

        understanding = TaskUnderstanding(
            raw_task=task,
            intent_summary=_summarise(intents, sheets[0]),
            interpretation=(
                InterpretationVerdict.SUFFICIENTLY_CLEAR
                if not missing
                else InterpretationVerdict.REQUIRES_USER_INPUT
            ),
            missing_information=missing,
            referenced_sheets=[target_sheet.name],
            referenced_columns=sorted({column for intent in intents for column in intent.columns}),
            planner_source=self.source,
        )

        return ExecutionPlan(
            plan_id=f"plan-{inspection.content_hash[:8]}",
            run_id="pending",
            understanding=understanding,
            operations=operations,
            source_hash=inspection.content_hash,
            planner_source=self.source,
            notes=notes,
        )

    def _build(
        self,
        intent: Intent,
        sheet: SheetMetadata,
        inspection: WorkbookInspection,
    ) -> tuple[list[WorkbookOperation], list[str], list[str]]:
        """Build the operations for one intent against one sheet."""
        target = Target(sheet=sheet.name)
        operations: list[WorkbookOperation] = []
        missing: list[str] = []
        notes: list[str] = []

        if intent.name == "normalize":
            columns = _resolve_columns(intent.columns, sheet)
            if intent.columns and not columns:
                missing.append(
                    f"none of the named columns ({', '.join(intent.columns)}) exist on "
                    f"{sheet.name!r}; available: {', '.join(sheet.header_row[:20])}"
                )
                return operations, missing, notes
            operations.append(
                NormalizeValues(
                    target=target,
                    columns=columns,
                    rules=NormalizeRules(
                        trim_whitespace=True,
                        collapse_internal_whitespace=True,
                        case="title" if not columns else "none",
                    ),
                )
            )
            notes.append(
                f"normalise applied to {len(columns) or 'all'} column(s) on {sheet.name!r}"
            )

        elif intent.name == "remove_duplicates":
            # Destructive: resolved under the strict rule in _resolve_dedupe_keys.
            keys, refusal = _resolve_dedupe_keys(intent.phrase, sheet)
            if refusal:
                missing.append(refusal)
                return operations, missing, notes
            operations.append(RemoveDuplicates(target=target, keys=keys))
            notes.append(
                f"duplicate removal keyed on {keys}"
                if keys
                else "duplicate removal on whole rows (no key was named, so only exact "
                "whole-row matches are removed)"
            )

        elif intent.name == "sort":
            keys = _resolve_columns(intent.columns or intent.sort_by, sheet)
            if not keys:
                missing.append(
                    f"sort column not identified on {sheet.name!r}; "
                    f"name a column, e.g. 'sort by Amount'"
                )
                return operations, missing, notes
            operations.append(
                SortRange(
                    target=target, by_columns=keys, descending=[intent.descending] * len(keys)
                )
            )

        elif intent.name == "filter":
            if not intent.columns:
                missing.append(
                    f"filter condition not specified for {sheet.name!r}; "
                    f"name a column and a value, e.g. 'filter Region = North'"
                )
                return operations, missing, notes
            conditions = []
            for column in intent.columns:
                resolved = _resolve_columns((column,), sheet)
                if not resolved:
                    missing.append(f"filter column {column!r} not found on {sheet.name!r}")
                    continue
                conditions.append(
                    FilterCondition(column=resolved[0], operator="not_equals", value="")
                )
            if conditions:
                operations.append(
                    FilterRows(
                        target=target,
                        conditions=conditions,
                        output_sheet=f"{sheet.name}_Filtered",
                        # Copy rather than delete: filtering is reversible this way.
                        hide_non_matching=False,
                    )
                )
                notes.append(
                    f"filtered rows written to a new sheet rather than deleted from {sheet.name!r}"
                )

        elif intent.name == "summarize":
            group = _resolve_columns(intent.columns or ("region", "product", "category"), sheet)
            if not group:
                missing.append(
                    f"no summarisable grouping column found on {sheet.name!r}; "
                    f"available: {', '.join(sheet.header_row[:20])}"
                )
                return operations, missing, notes
            measure_column = _first_matching(sheet, COLUMN_SYNONYMS["amount"]) or (
                group[0] if group else None
            )
            if measure_column is None:
                missing.append(f"no numeric column found on {sheet.name!r} to measure")
                return operations, missing, notes
            output = "Summary"
            if output in inspection.sheet_names:
                output = f"Summary_{sheet.name}"
            operations.append(
                CreateSummary(
                    target=target,
                    output_sheet=output,
                    spec=SummarySpec(
                        group_by=group[:1],
                        measures=[Measure(column=measure_column, aggregation="sum")],
                        row_limit=intent.limit,
                    ),
                )
            )
            notes.append(f"summary written to {output!r}")

        elif intent.name == "validate":
            resolved = _resolve_columns(intent.columns, sheet)
            if not resolved:
                # No column named: fall back to the identifier-like column, which
                # is the one whose completeness actually matters.
                fallback = _first_matching(sheet, COLUMN_SYNONYMS["invoice"])
                resolved = [fallback] if fallback else []
            rules = [
                rule
                for rule in (_validation_rule_for(column) for column in resolved)
                if rule is not None
            ]
            if not rules:
                missing.append(f"no column on {sheet.name!r} could be checked for completeness")
                return operations, missing, notes
            operations.append(ApplyValidation(target=target, rules=rules, report_only=True))
            notes.append("validation is report-only; nothing is written")

        elif intent.name == "read":
            operations.append(ReadRange(target=target, max_rows=50))

        return operations, missing, notes

    def _refusal(
        self,
        task: UntrustedText,
        inspection: WorkbookInspection,
        reason: str,
        missing: list[str],
    ) -> ExecutionPlan:
        """Build a plan-shaped refusal.

        A refusal is still a well-formed plan carrying
        ``REQUIRES_USER_INPUT``, which policy denies (``no_guessing``). Returning
        this shape rather than raising keeps the pipeline uniform: the run reaches
        the approval gate, the operator is told exactly what is missing, and the
        refusal is audited like any other outcome.
        """
        understanding = TaskUnderstanding(
            raw_task=task,
            intent_summary=f"cannot proceed: {reason}",
            interpretation=InterpretationVerdict.REQUIRES_USER_INPUT,
            # Both the specific gap and the actionable guidance, so the operator
            # learns what to do rather than just what went wrong.
            missing_information=[*missing, UNSUPPORTED_GUIDANCE.get(reason, "")],
            planner_source=self.source,
        )
        # A single harmless read keeps the plan non-empty, since ExecutionPlan
        # requires at least one operation. Policy denies before it executes.
        return ExecutionPlan(
            plan_id=f"refusal-{inspection.content_hash[:8]}",
            run_id="pending",
            understanding=understanding,
            operations=[ReadRange(target=Target(sheet=inspection.sheet_names[0]), max_rows=1)],
            source_hash=inspection.content_hash,
            planner_source=self.source,
            notes=[f"refused: {reason}"],
        )


#: The keyword each intent is anchored on, used to find its local context.
_INTENT_ANCHOR: dict[str, str] = {
    "remove_duplicates": r"duplicat|dedup|de-?dup",
    "normalize": r"normalis|normaliz|clean|trim|standardis|standardiz|tidy",
    "sort": r"\bsort\b|\border\b",
    "filter": r"\bfilter\b|\bonly\b|\bwhere\b",
    "summarize": r"summar|pivot|breakdown|aggregat|total",
    "validate": r"validat|missing|required",
    "read": r"^\s*(read|show|list|display|inspect|report)",
}

#: How far each intent's context extends either side of its anchor, in
#: characters. Wide enough for "remove duplicate records by InvoiceId", narrow
#: enough that a neighbouring clause's columns do not leak in.
_INTENT_WINDOW = 60


def _local_context(text: str, anchor: str) -> str:
    """The slice of the request belonging to one intent.

    This is what keeps a compound request from cross-contaminating. In
    "normalise the Customer column, remove duplicate invoices, and create a
    summary by Region", the dedupe clause must not inherit Region or Customer.
    Taking columns from the whole request is exactly what produced a dedupe key of
    ``[Region, Customer]`` - a valid pair of real columns, so it passed every
    validation, while collapsing 38 of 42 rows.
    """
    match = re.search(anchor, text, re.IGNORECASE)
    if match is None:
        return text
    start = max(0, match.start() - _INTENT_WINDOW)
    end = min(len(text), match.end() + _INTENT_WINDOW)
    return text[start:end]


def _detect_intents(text: str) -> list[Intent]:
    """Collect every recognised intent, de-duplicated, in a stable order."""
    found: list[Intent] = []
    seen: set[str] = set()
    for name, pattern in _INTENT_PATTERNS:
        if not pattern.search(text):
            continue
        key = "remove_duplicates" if name == "dedupe_keyed" else name
        if key in seen:
            continue
        seen.add(key)
        local = _local_context(text, _INTENT_ANCHOR.get(key, r".*?"))
        local_columns = _mentioned_columns(local)
        found.append(
            Intent(
                name=key,
                columns=local_columns,
                sort_by=local_columns if key == "sort" else (),
                descending=bool(
                    re.search(r"\bdescending\b|\bhighest\b|\blargest\b|\bnewest\b", local, re.I)
                ),
                limit=int(m.group(1)) if (m := _LIMIT.search(local)) else None,
                phrase=local,
            )
        )
    return found


def _candidate_sheets(text: str, inspection: WorkbookInspection) -> list[_SheetCandidate]:
    """Rank sheets by how explicitly the request names them.

    Three tiers of evidence, deliberately distinguished:

    1. **Quoted** - ``'Sales'`` or ``"Sales"``. Unambiguous.
    2. **Introduced** - "on the Sales sheet", "sheet Sales". Unambiguous.
    3. **Capitalised bare mention** - the name appears as a capitalised whole word.

    Tier 3 requires capitalisation because a sheet name is a proper noun in a
    request ("on Sales"), whereas an operation word is not. That is what stops a
    sheet literally named ``Summary`` from capturing the request *"create a summary
    by Region"*, where "summary" names the operation rather than the target. This
    was a real bug, found by a test.

    When no sheet is named, the largest sheet is chosen and the plan's intent
    summary records that it was a default rather than an instruction.
    """
    scored: list[_SheetCandidate] = []
    quoted = {name.strip().lower() for name in _SHEET_QUOTED.findall(text)}
    introduced = {match.strip().lower() for match in _SHEET_AFTER_PREPOSITION.findall(text)}

    for sheet in inspection.sheets:
        name = sheet.name.strip()
        lowered_name = name.lower()
        score = 0
        explicit = False

        if lowered_name in quoted:
            score += 100
            explicit = True
        elif lowered_name in introduced:
            score += 80
            explicit = True
        elif _mentions_word(text, name):
            score += 30
            explicit = True
        else:
            score += min(len(name), 30) // 10

        if not sheet.is_visible:
            # Prefer a visible sheet, but an explicitly named hidden sheet wins.
            score -= 20 if not explicit else 0

        # Data volume breaks ties, so an unnamed request lands on the sheet that
        # actually holds the records rather than on a small reference table.
        score += min(sheet.max_row, 10_000) // 100
        scored.append(_SheetCandidate(sheet=sheet, matched_explicitly=explicit, score=score))

    scored.sort(key=lambda candidate: candidate.score, reverse=True)
    return scored


def _mentions_word(text: str, name: str) -> bool:
    """Whether the request names a sheet as a **capitalised** whole word.

    Two conditions, both necessary:

    * **Word boundaries**, so a sheet named ``Sales`` is not matched inside
      "wholesale", nor ``Data`` inside "metadata".
    * **Capitalisation**, because a sheet name is a proper noun in a request
      ("on Sales"), whereas an operation word is not.

    Requests that name a sheet in lowercase ("on sales") still work: the quoted and
    introduced tiers catch those.
    """
    if not name:
        return False
    for match in re.finditer(rf"(?<!\w)({re.escape(name)})(?!\w)", text):
        if match.group(1)[:1].isupper():
            return True
    return False


def _mentioned_columns(text: str) -> tuple[str, ...]:
    """Column names the request names, via "by X" or "the X column".

    Returns them in order, de-duplicated, case-insensitively.
    """
    found: list[str] = []
    seen: set[str] = set()
    for pattern in (_BY_COLUMN, _COLUMN_MENTION):
        for raw in pattern.findall(text):
            name = raw.strip().strip("'\"`").strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                found.append(name)
    return tuple(found)


def _resolve_dedupe_keys(text: str, sheet: SheetMetadata) -> tuple[list[str], str | None]:
    """Resolve the duplicate key, refusing to guess.

    A duplicate key is **destructive**: a wrong one silently deletes rows that
    were not duplicates. This is the most dangerous inference available to the
    planner, so it follows a stricter rule than every other intent:

    * a key named in the dedupe clause is used, if it resolves
      ("remove duplicates by InvoiceId");
    * otherwise a concept named in that clause is honoured
      ("remove duplicate invoices" -> the invoice-id column);
    * otherwise the key is **not** inferred. A missing-information signal is
      returned and policy denies the run.

    Only the dedupe clause is consulted — never columns mentioned elsewhere in the
    request. An earlier version took them from anywhere, and on "normalise the
    Customer column, remove duplicate invoices, and create a summary by Region"
    produced the key ``[Region, Customer]``: a valid pair of real columns, so it
    passed every validation, while collapsing 38 of 42 rows. Policy escalated it
    and verification caught the row loss, but the correct fix is not to emit it.
    """
    tail = _text_after(text, r"duplicat|dedup|de-?dup")

    # 1. An explicit key clause: "duplicates by InvoiceId", "duplicate rows on OrderNo".
    explicit = re.search(
        r"\b(?:by|on|keyed\s+on|using|based\s+on|for)\s+['\"`]?([A-Za-z][A-Za-z0-9_.\-]{0,38})",
        tail,
        re.IGNORECASE,
    )
    if explicit:
        resolved = _resolve_columns((explicit.group(1),), sheet)
        if resolved:
            return resolved, None

    # 2. A concept named directly in the dedupe clause: "duplicate invoices".
    #
    # Deliberately not one clever regex. An earlier version stripped a leading
    # preposition, and the `in` alternative matched the first two letters of
    # "invoice", so "remove duplicate invoice records" resolved nothing. Taking
    # whole words and looking each up is simpler and correct.
    for token in _content_words(tail):
        synonyms = _CONCEPT_TOKENS.get(token.lower())
        if synonyms:
            match = _first_matching(sheet, synonyms)
            if match:
                return [match], None

    # 3. Nothing stated: refuse.
    return [], (
        f"the duplicate key column was not identified on {sheet.name!r}; ExcelPilot will "
        f"not guess a destructive key. Name it explicitly, for example "
        f"'remove duplicates by InvoiceId'. Available columns: "
        f"{', '.join(header for header in sheet.header_row if header) or '(none)'}"
    )


#: Words that describe the row, not the key. Skipped when looking for a concept.
_ROW_NOISE = frozenset(
    {
        "records",
        "record",
        "rows",
        "row",
        "entries",
        "entry",
        "values",
        "value",
        "on",
        "by",
        "in",
        "of",
        "the",
        "a",
        "an",
        "and",
        "then",
        "also",
        "all",
        "any",
        "from",
        "for",
        "with",
        "duplicate",
        "duplicates",
        "duplicated",
        "dedupe",
    }
)


def _text_after(text: str, anchor: str) -> str:
    """The clause following the first match of ``anchor``.

    Bounded at a **clause boundary** — a comma, "and", "then", or a full stop —
    not merely at a character count. A character bound is not enough: in
    "remove duplicate invoices, and create a summary by Region", the next 60
    characters still contain "by Region", so a window alone let a neighbouring
    clause supply this clause's dedupe key.
    """
    match = re.search(anchor, text, re.IGNORECASE)
    if match is None:
        return text
    tail = text[match.end() :]
    boundary = re.search(r"\s*(?:,|\.|;|\band\b|\bthen\b|\balso\b|\bbut\b)", tail, re.IGNORECASE)
    if boundary is not None:
        tail = tail[: boundary.start()]
    return tail[:_INTENT_WINDOW]


def _content_words(text: str, *, limit: int = 4) -> list[str]:
    """The first ``limit`` content words, skipping row-describing noise."""
    found: list[str] = []
    for word in re.findall(r"[A-Za-z][A-Za-z0-9_.\-]*", text):
        if word.lower() in _ROW_NOISE:
            continue
        found.append(word)
        if len(found) >= limit:
            break
    return found


def _resolve_columns(names: tuple[str, ...], sheet: SheetMetadata) -> list[str]:
    """Map requested column names onto the sheet's actual headers."""
    resolved: list[str] = []
    for name in names:
        index = sheet.find_header(name)
        if index is not None and 1 <= index <= len(sheet.header_row):
            actual = sheet.header_row[index - 1]
            if actual and actual not in resolved:
                resolved.append(actual)
            continue
        # Fall back to the synonym table for requests like "the customer column".
        for synonyms in COLUMN_SYNONYMS.values():
            if name.strip().lower() in synonyms:
                for synonym in synonyms:
                    synonym_index = sheet.find_header(synonym)
                    if synonym_index is not None:
                        actual = sheet.header_row[synonym_index - 1]
                        if actual and actual not in resolved:
                            resolved.append(actual)
                        break
                if len(resolved) > len(names) - 1:
                    break
    return resolved


def _first_matching(sheet: SheetMetadata, synonyms: tuple[str, ...]) -> str | None:
    """First header on the sheet matching any synonym."""
    for synonym in synonyms:
        index = sheet.find_header(synonym)
        if index is not None:
            return sheet.header_row[index - 1]
    return None


def _validation_rule_for(column: str) -> object | None:
    """Pick a sensible validation rule for a column, based on its name."""
    from app.contracts.operations import ValidationRule

    lowered = column.lower()
    if any(token in lowered for token in ("id", "invoice", "ref", "number", "code")):
        return ValidationRule(column=column, rule="not_empty")
    if any(token in lowered for token in ("email", "mail")):
        return ValidationRule(column=column, rule="email")
    if any(token in lowered for token in ("amount", "total", "price", "qty", "quantity")):
        return ValidationRule(column=column, rule="numeric")
    return ValidationRule(column=column, rule="not_empty")


def _summarise(intents: list[Intent], candidate: _SheetCandidate) -> str:
    """One-line human summary of what the plan will do."""
    actions = ", ".join(sorted({intent.name.replace("_", " ") for intent in intents}))
    basis = (
        "named in the request"
        if candidate.matched_explicitly
        else "chosen as the largest sheet; name it explicitly to target another"
    )
    return f"{actions} on sheet {candidate.sheet.name!r} ({basis})"


__all__ = [
    "COLUMN_SYNONYMS",
    "DeterministicPlanner",
    "Intent",
    "Planner",
]
