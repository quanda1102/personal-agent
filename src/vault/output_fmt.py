"""Machine-first OK/ERR lines for vault CLI responses."""

from __future__ import annotations

from typing import Any

from ..cli_handler.result import Result, ok, err


def vault_ok(**fields: Any) -> str:
    lines = ["OK"]
    for k, v in fields.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            v = ",".join(str(x) for x in v)
        lines.append(f"{k}: {v}")
    return "\n".join(lines)


def vault_err(code: str, message: str, hint: str = "") -> str:
    lines = ["ERR", f"code: {code}", f"message: {message}"]
    if hint:
        lines.append(f"hint: {hint}")
    return "\n".join(lines)


def result_ok(**fields: Any) -> Result:
    return ok(vault_ok(**fields))


def result_err(code: str, message: str, hint: str = "", exit_code: int = 1) -> Result:
    return err(vault_err(code, message, hint=hint), exit=exit_code)
