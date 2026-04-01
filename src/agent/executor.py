"""
openclawd.core.loop.executor
─────────────────────────────
Executor abstraction — how commands actually run.

The LLM composes a command string.
The Runner passes it to an Executor.
The Executor decides WHERE and HOW it runs.
The LLM never knows the difference.

                    LLM: run("cat /openclawd/memory/facts.md")
                              ↓
                    Runner → executor.exec("cat /openclawd/memory/facts.md")
                              ↓
          LocalExecutor: engine_run()      ← runs on this machine
          SSHExecutor:   ssh user@host ... ← runs on remote machine
                              ↓
                    Result(stdout="user_name: Hung\n...", exit=0)
                              ↓
                    LLM sees: "user_name: Hung\n[exit:0 | 45ms]"
                              ← SSH passkey NEVER in this string

SSHExecutor holds credentials. They live in RunContext (system-only).
The LLM never sees RunContext. It only ever sees command output.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..cli_handler.result import Result


class Executor(ABC):
    """
    Abstract command executor.
    One method: exec(command) → Result.
    That's the entire contract.
    """

    @abstractmethod
    async def exec(self, command: str) -> Result:
        """
        Execute a command string and return a Result.
        Never raises — errors are captured in Result(exit=1).
        """
        ...

    @property
    @abstractmethod
    def location(self) -> str:
        """Human-readable description of where commands run (for logging)."""
        ...


# ── Local executor ─────────────────────────────────────────────────────────────

class LocalExecutor(Executor):
    """
    Runs commands on the local machine via engine.run().
    Default executor — no credentials needed.
    """

    async def exec(self, command: str) -> Result:
        from ..cli_handler.router import run as engine_run
        return engine_run(command)

    @property
    def location(self) -> str:
        return "local"


class RoleScopedExecutor(Executor):
    """
    Sets EXECUTION_ROLE for the duration of each exec() call.

    Use ROLE_CONVERSATION for chat/voice/API; use ROLE_HEARTBEAT for scheduled
    vault work. Inner executor defaults to LocalExecutor.
    """

    def __init__(self, role: str, inner: Executor | None = None) -> None:
        from .exec_role import EXECUTION_ROLE

        self._role_var = EXECUTION_ROLE
        self._role = role
        self._inner = inner or LocalExecutor()

    async def exec(self, command: str) -> Result:
        token = self._role_var.set(self._role)
        try:
            return await self._inner.exec(command)
        finally:
            self._role_var.reset(token)

    @property
    def location(self) -> str:
        return f"{self._inner.location}[role={self._role!r}]"


# ── SSH executor ───────────────────────────────────────────────────────────────

class SSHExecutor(Executor):
    """
    Runs commands on a remote machine over SSH.

    Credentials live here — completely invisible to the LLM.
    The LLM never sees host, user, key_path, or passphrase.
    It only ever sees the stdout of commands it runs.

    Args:
        host:       Remote hostname or IP
        user:       SSH username
        key_path:   Path to private key file (default: ~/.ssh/id_rsa)
        port:       SSH port (default: 22)
        timeout:    Command timeout in seconds (default: 30)
        base_path:  Working directory on remote (default: /openclawd)

    NOTE: Full implementation pending asyncssh or paramiko integration.
    Scaffold is here so RunContext can carry it today;
    replace _exec_remote() when SSH transport is wired up.
    """

    def __init__(
        self,
        host:      str,
        user:      str,
        key_path:  str | None = None,
        port:      int = 22,
        timeout:   int = 30,
        base_path: str = "/openclawd",
    ):
        # ── Credentials — system-only, never logged or emitted ──
        self._host      = host
        self._user      = user
        self._key_path  = key_path
        self._port      = port
        self._timeout   = timeout
        self._base_path = base_path

    async def exec(self, command: str) -> Result:
        import time

        t0 = time.perf_counter()
        try:
            stdout, exit_code = await self._exec_remote(command)
            elapsed = (time.perf_counter() - t0) * 1000
            return Result(stdout=stdout, exit=exit_code, elapsed_ms=elapsed)
        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            return Result(
                stdout=f"[error] ssh exec failed: {e}",
                exit=1,
                elapsed_ms=elapsed,
            )

    async def _exec_remote(self, command: str) -> tuple[str, int]:
        """
        ── SWAP POINT ──
        Replace this with asyncssh or paramiko implementation.

        asyncssh example:
            import asyncssh
            async with asyncssh.connect(
                self._host, port=self._port,
                username=self._user,
                client_keys=[self._key_path],
                known_hosts=None,
            ) as conn:
                result = await conn.run(command, timeout=self._timeout)
                return result.stdout, result.exit_status
        """
        raise NotImplementedError(
            "SSHExecutor._exec_remote() not yet wired. "
            "Install asyncssh and implement this method."
        )

    @property
    def location(self) -> str:
        return f"ssh://{self._user}@{self._host}:{self._port}"

    def __repr__(self) -> str:
        # Safe repr — never exposes key_path or credentials
        return f"SSHExecutor(host={self._host!r}, user={self._user!r}, port={self._port})"