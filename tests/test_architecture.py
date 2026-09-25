"""Architecture boundary tests.

These are load-bearing. The safety model described in the ADRs depends on the
dependency graph actually holding, so it is enforced by static import analysis
rather than by convention or review.

If one of these fails, the corresponding ADR has to be revisited deliberately —
not worked around.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent / "app"


def _python_files(package: str) -> Iterator[Path]:
    yield from sorted((APP_ROOT / package).rglob("*.py"))


def _imported_modules(path: Path) -> set[str]:
    """Every ``app.*`` module imported by a file, from AST — not from runtime."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app."):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Relative import inside app/. Resolve it against the file's package.
                parts = path.relative_to(APP_ROOT).parts
                prefix = ".".join(parts[:-1]) if node.module is None else ".".join(parts[:-1])
                found.add(f"{prefix}.{node.module}" if node.module else prefix)
                for alias in node.names:
                    found.add(
                        f"{prefix}.{node.module}.{alias.name}"
                        if node.module
                        else f"{prefix}.{alias.name}"
                    )
            elif node.module and node.module.startswith("app."):
                found.add(node.module)
                for alias in node.names:
                    found.add(f"{node.module}.{alias.name}")
    return found


def _violations(package: str, forbidden_prefixes: tuple[str, ...]) -> list[str]:
    problems: list[str] = []
    for path in _python_files(package):
        for imported in _imported_modules(path):
            head = ".".join(imported.split(".")[:2])
            if any(
                head == prefix or head.startswith(f"{prefix}.") for prefix in forbidden_prefixes
            ):
                problems.append(f"{path.relative_to(APP_ROOT)} imports {imported}")
    return problems


class TestContractsAreTheBase:
    def test_contracts_imports_nothing_outside_contracts(self) -> None:
        """Contracts may reference each other, but must not reach any other layer."""
        problems: list[str] = []
        for path in _python_files("contracts"):
            for imported in _imported_modules(path):
                if not imported.startswith("app.contracts"):
                    problems.append(f"{path.relative_to(APP_ROOT)} imports {imported}")
        assert problems == [], f"contracts must be self-contained: {problems}"

    def test_contracts_performs_no_io(self) -> None:
        """Contracts describe data. They must not touch the filesystem or network.

        Validators, properties and derived helpers are expected; I/O is not. This
        is the check that keeps the base of the dependency graph inert.

        One narrow, documented exception: ``config.py`` imports ``pathlib`` so
        ``ExcelPilotConfig.load()`` can read an operator's TOML file. That is the
        single boundary where external input enters the process, so it is allowed
        there and forbidden everywhere else in the package. Network access and
        subprocess access remain prohibited outright, including in config.py.
        """
        always_forbidden = {"urllib", "http", "socket", "subprocess", "requests", "httpx"}
        path_exceptions = {"config.py"}
        offenders: list[str] = []
        for path in _python_files("contracts"):
            forbids_paths = path.name not in path_exceptions
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        if root in always_forbidden or (
                            forbids_paths and root in {"pathlib", "os", "shutil"}
                        ):
                            offenders.append(
                                f"{path.relative_to(APP_ROOT)}:{node.lineno} imports {alias.name}"
                            )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".")[0]
                    if root in always_forbidden or (
                        forbids_paths and root in {"pathlib", "os", "shutil"}
                    ):
                        offenders.append(
                            f"{path.relative_to(APP_ROOT)}:{node.lineno} imports {node.module}"
                        )
        assert offenders == [], f"contracts must not perform I/O: {offenders}"


class TestLayerBoundaries:
    @pytest.mark.parametrize(
        ("package", "forbidden", "reason"),
        [
            (
                "workbook",
                ("app.planner", "app.decisions", "app.policy", "app.executor"),
                "the workbook engine must not depend on AI, JEV, or policy",
            ),
            (
                "policy",
                ("app.planner", "app.decisions", "app.executor", "app.cli"),
                "policy is the sole authority; it must not consult a model (ADR-0005)",
            ),
            (
                "executor",
                ("app.planner", "app.decisions", "app.cli", "app.dashboard"),
                "the executor receives a validated plan, never a model (ADR-0006)",
            ),
            (
                "diff",
                ("app.planner", "app.decisions", "app.cli"),
                "diff is computed from content, not from opinions",
            ),
            (
                "audit",
                ("app.planner", "app.decisions", "app.cli"),
                "audit records independently of what produced the event",
            ),
            (
                "decisions",
                ("app.executor", "app.policy", "app.workbook"),
                "JEV is advisory and cannot reach the workbook (ADR-0004)",
            ),
        ],
    )
    def test_layer_does_not_import_forbidden(
        self, package: str, forbidden: tuple[str, ...], reason: str
    ) -> None:
        assert _violations(package, forbidden) == [], reason

    def test_only_orchestrator_and_ui_compose_layers(self) -> None:
        """policy/executor/verification must be composed by app/, not by each other."""
        for package in ("policy", "executor", "verification"):
            for path in _python_files(package):
                text = path.read_text(encoding="utf-8")
                for other in ("app.app", "app.cli", "app.dashboard"):
                    assert other not in text, f"{path.relative_to(APP_ROOT)} composes {other}"


class TestNoDynamicExecution:
    """No eval/exec/compile anywhere: arbitrary code execution is not implemented (spec section 24)."""

    #: Bare-name calls that execute code dynamically.
    FORBIDDEN_CALLS = frozenset({"eval", "exec", "compile", "__import__"})

    #: Attribute calls that execute code dynamically.
    FORBIDDEN_ATTRIBUTE_CALLS = frozenset({"eval", "exec", "__import__"})

    def test_no_dynamic_execution_anywhere(self) -> None:
        offenders: list[str] = []
        for path in APP_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in self.FORBIDDEN_CALLS
                ):
                    offenders.append(f"{path.relative_to(APP_ROOT)}:{node.lineno} {node.func.id}()")
        assert offenders == [], f"dynamic execution found: {offenders}"

    def test_no_dynamic_execution_via_attribute(self) -> None:
        """Catch `builtins.eval(...)` and friends reached through an attribute.

        Deliberately narrower than :attr:`FORBIDDEN_CALLS`: a blanket
        ``.compile()`` ban would also flag legitimate re-compiled ``re.Pattern``
        use, and openpyxl's loosely-typed objects genuinely require defensive
        attribute probing. What matters is executing a name resolved at runtime,
        which the next test covers directly.
        """
        offenders: list[str] = []
        for path in APP_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in self.FORBIDDEN_ATTRIBUTE_CALLS
                ):
                    offenders.append(
                        f"{path.relative_to(APP_ROOT)}:{node.lineno} .{node.func.attr}()"
                    )
        assert offenders == [], f"dynamic execution found: {offenders}"

    def test_builtins_are_never_reached_dynamically(self) -> None:
        """Block the realistic ways to evade the two checks above.

        ``getattr(__builtins__, "eval")`` and ``getattr(cell, "__class__")`` are
        the practical escape hatches. Ordinary defensive
        ``getattr(obj, "attr", default)`` with a *literal* name stays allowed — it
        probes a library object, it does not resolve executable code.
        """
        offenders: list[str] = []
        for path in APP_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                    continue
                if node.func.id != "getattr" or len(node.args) < 2:
                    continue
                target_text = ast.unparse(node.args[0])
                name = ast.unparse(node.args[1])
                if "__builtins__" in target_text or target_text == "builtins":
                    offenders.append(
                        f"{path.relative_to(APP_ROOT)}:{node.lineno} getattr({target_text}, {name})"
                    )
                elif name.startswith("__") and name.endswith("__"):
                    offenders.append(
                        f"{path.relative_to(APP_ROOT)}:{node.lineno} getattr(..., {name})"
                    )
        assert offenders == [], f"builtins reached dynamically: {offenders}"


class TestNoSubprocessToExternalServices:
    """The executor must not shell out. Network lives in app/net only."""

    def test_executor_has_no_subprocess(self) -> None:
        for path in _python_files("executor"):
            text = path.read_text(encoding="utf-8")
            for forbidden in ("subprocess", "os.system", "os.popen", "urllib", "http.client"):
                assert forbidden not in text, f"{path.relative_to(APP_ROOT)} references {forbidden}"


class TestNoPrintInLibraryCode:
    def test_library_code_does_not_print(self) -> None:
        """Printing belongs to the CLI. Library code returns data."""
        offenders: list[str] = []
        for package in (
            "workbook",
            "policy",
            "executor",
            "verification",
            "diff",
            "audit",
            "decisions",
            "planner",
            "safety",
            "storage",
        ):
            for path in _python_files(package):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "print"
                    ):
                        offenders.append(f"{path.relative_to(APP_ROOT)}:{node.lineno}")
        assert offenders == [], f"print() in library code: {offenders}"
