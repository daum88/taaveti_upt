"""Architectural import-layer guardrails.

Dependencies must point inward: web/runtime may depend on application, application may
depend on domain, and domain stays pure. These rules are enforced statically by parsing
import statements so the test never imports the checked modules or triggers side effects.
"""

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def _python_files(package: str) -> list[Path]:
    return sorted(p for p in (PROJECT_ROOT / package).rglob("*.py") if "__pycache__" not in p.parts)


def _matches(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes)


def _violations(package: str, forbidden: tuple[str, ...]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for path in _python_files(package):
        bad = {module for module in _imported_modules(path) if _matches(module, forbidden)}
        if bad:
            result[str(path.relative_to(PROJECT_ROOT))] = bad
    return result


def test_domain_is_pure() -> None:
    """Domain modules import only standard library and other domain modules."""
    forbidden = (
        "fastapi",
        "starlette",
        "uvicorn",
        "sqlite3",
        "yfinance",
        "pandas",
        "requests",
        "config",
        "adapters",
        "services",
        "application",
        "models",
        "db",
        "ui",
    )
    assert _violations("domain", forbidden) == {}


def test_application_does_not_depend_on_web_or_ui() -> None:
    """Application modules never import the web adapter, HTTP framework, or presentation layer."""
    forbidden = (
        "fastapi",
        "starlette",
        "uvicorn",
        "adapters.web",
        "ui",
        "sqlite3",
        "config",
    )
    assert _violations("application", forbidden) == {}
