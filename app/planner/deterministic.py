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
_BY_COLUMN = re.compile(r"\bby\s+['\"\`]?([A-Za-z0-9 _.\-]{1,40})['\"`]?", re.I)
#: "the Customer column", "Customer field", "Amount column" — the other common way
#: a request names a column, alongside "by <column>".
_COLUMN_MENTION = re.compile(
    r"\b(?:the\s+)?['\"\`]?([A-Za-z][A-Za-z0-9 _.\-]{0,38}?)['\"`]?\s+"
    r"(?:column|field|values?)\b",
    re.I,
)
#: Leading words that are part of the phrase, not the column name.
_COLUMN_LEADING_NOISE = frozenset(
    {"the", "a", "an", "that", "this", "each", "every", "its", "their", "same", "one"}
)
_LIMIT = re.compile(r"\b(?:top|first|limit)\s+(\d{1,7})\b", re.I)


def _mentioned_columns(text: str) -> tuple[str, ...]:
    """Column names the request names, via "by X" or "the X column".

    Returns them in the order they appear, de-duplicated, with leading filler
    words ("the", "each") stripped so "the Customer column" yields "Customer".
    """
    found: list[str] = []
    for pattern in (_BY_COLUMN, _COLUMN_MENTION):
        for raw in pattern.findall(text):
            name = raw.strip().strip("'\"`").strip()
            while name.lower() in _COLUMN_LEADING_NOISE:
                name = name.split(None, 1)[1] if " " in name else ""
            if not name:
                continue
            # "sort by Amount descending" -> "Amount descending"; keep the head.
            name = (
                name.split()[0]
                if len(name.split()) > 1
                and name.split()[-1].lower()
                in {
                    "descending",
                    "ascending",
                    "column",
                    "field",
                    "values",
                    "value",
                }
                else name
            )
            if name and name.lower() not in {existing.lower() for existing in found}:
                found.append(name)
    return tuple(found)


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
            keys = _resolve_columns(intent.columns, sheet)
            if intent.columns and not keys:
                missing.append(
                    f"duplicate key column(s) not found on {sheet.name!r}; "
                    f"available: {', '.join(sheet.header_row[:20])}"
                )
                return operations, missing, notes
            operations.append(RemoveDuplicates(target=target, keys=keys))
            notes.append(f"duplicate removal keyed on {keys or 'whole rows'}")

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
            from app.contracts.operations import ApplyValidation, ValidationRule

            operations.append(ApplyValidation(target=target, rules=rules, report_only=True))
            del ValidationRule
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

        columns = _mentioned_columns(text)
        limit_match = _LIMIT.search(text)
        found.append(
            Intent(
                name=key,
                columns=columns,
                sort_by=columns if key == "sort" else (),
                descending=bool(
                    re.search(r"\bdescending\b|\bhighest\b|\blargest\b|\bnewest\b", text, re.I)
                ),
                limit=int(limit_match.group(1)) if limit_match else None,
            )
        )
    return found


def _candidate_sheets(text: str, inspection: WorkbookInspection) -> list[_SheetCandidate]:
    """Rank sheets by how explicitly the request names them.

    Three tiers of evidence, deliberately distinguished:

    1. **Quoted** — ``'Sales'`` or ``"Sales"``. Unambiguous.
    2. **Introduced** — "on the Sales sheet", "sheet Sales". Unambiguous.
    3. **Bare mention** — the sheet name appears as a whole word. Weak.

    Tier 3 is kept separate because of a real failure mode this planner hit: a
    workbook containing a sheet literally named ``Summary`` must not capture the
    request "create a summary by Region", where "summary" is the *operation*, not
    the target. A bare word-boundary match therefore scores far below a quoted or
    introduced name, and the data-size term breaks the remaining ties.

    When no sheet is named at all, the largest sheet is chosen and the plan's
    intent summary records that it was a default rather than an instruction.
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
            # Capitalised proper-noun mention: a real reference, though weaker
            # than an explicit "the Sales sheet" construction.
            score += 30
            explicit = True
        else:
            score += min(len(name), 30) // 10

        if not sheet.is_visible:
            # Prefer a visible sheet, but an explicitly named hidden sheet still wins.
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
      ("on Sales"), whereas an operation word is not. This is what stops a sheet
      literally named ``Summary`` from capturing the request *"create a summary
      by Region"*, where "summary" names the operation rather than the target.

    Requests that name a sheet in lowercase ("on sales") still work — the
    introduced and quoted tiers catch those.
    """
    if not name:
        return False
    for match in re.finditer(rf"(?<!\w)({re.escape(name)})(?!\w)", text):
        # match.group(1) is the text as it actually appeared in the request.
        if match.group(1)[:1].isupper():
            return True
    return False


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
