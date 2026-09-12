"""Everything a wizard step is allowed to reach, and the seams tests replace.

Three rules shape this object, and they are the doctor's rules for the same
reasons.

**Every dependency is lazy.** ``genus init`` runs on a box where nothing works
yet: no database, no settings file that parses, no engine. Importing this
module must therefore need none of them, and nothing here connects, imports
psycopg2 or resolves settings until a step asks.

**Questions go through one seam.** ``ask()`` is the only way a step may learn
something from the operator. With ``--yes`` it returns the default without
touching stdin, which is what makes the non-interactive path testable and what
stops a step from blocking a provisioning script on a prompt nobody will
answer.

**Human text and machine text never share a stream.** In ``--json`` mode the
document on stdout is the whole contract, so ``say()`` writes to stderr. A
step that used ``print()`` would corrupt the payload for every consumer.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from robothor.doctor.context import HttpResponse

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from robothor.settings.model import GenusSettings

__all__ = ["HttpResponse", "InitContext"]

#: Seconds any one probe the wizard makes may take. Short on purpose: phase 1
#: runs every check before it writes anything, and an operator waiting on a
#: plan is waiting on the slowest check in it.
DEFAULT_HTTP_TIMEOUT_S = 5.0


def _default_fetch(
    method: str, url: str, body: dict[str, Any] | None, timeout: float
) -> HttpResponse:
    """The real HTTP call. Never raises -- a dead endpoint is a status of 0."""
    try:
        import httpx

        response = httpx.request(method, url, json=body, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - the failure IS the answer here
        return HttpResponse(status=0, error=f"{type(exc).__name__}: {exc}")
    return HttpResponse(status=response.status_code, body=response.text)


@dataclass
class InitContext:
    """What a step may reach, and what it may write.

    Args:
        workspace: the instance directory being created. Every path a step
            writes is under it or under ``~/.robothor``.
        answers: what the operator has already told us, by answer key. Seeded
            from flags and environment so ``--yes`` never has to ask.
        dry_run: run every ``check()``, apply nothing, write no state.
        json_mode: human text goes to stderr so stdout carries one document.
        yes: take defaults instead of prompting.
        offline: do not leave the box or spend money. The provider probe
            records its choice unprobed and says so.
        substrate_name: which substrate's steps are in the plan.
        prompt: the question seam. ``None`` with ``yes=False`` reads stdin.
        http_fetch: the HTTP seam, replaced wholesale in tests.
        settings_factory: the settings seam. Defaults to ``get_settings``.
    """

    workspace: Path
    answers: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    json_mode: bool = False
    yes: bool = False
    offline: bool = False
    substrate_name: str = "local"
    prompt: Callable[[str, str], str] | None = None
    http_fetch: Callable[[str, str, dict[str, Any] | None, float], HttpResponse] | None = None
    settings_factory: Callable[[], Any] | None = None
    db_factory: Callable[[], Any] | None = None
    stream: TextIO | None = None

    #: Detail each applied step wants in the report, by step id. A step sets
    #: it through :meth:`detail`; the runner reads it when the step returns.
    details: dict[str, str] = field(default_factory=dict)
    #: The URL the ``link`` step minted, if it ran.
    first_run_url: str = ""

    _settings: Any = field(default=None, repr=False)
    _state: dict[str, str] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).expanduser()

    # -- settings ---------------------------------------------------------

    @property
    def settings(self) -> GenusSettings:
        """The typed settings, resolved once and cached for the whole run."""
        if self._settings is None:
            factory = self.settings_factory
            if factory is None:
                from robothor.settings import get_settings

                factory = get_settings
            self._settings = factory()
        settings: GenusSettings = self._settings
        return settings

    # -- questions --------------------------------------------------------

    def ask(self, key: str, question: str, default: str = "") -> str:
        """Answer ``key``, asking only when we do not already know.

        An answer supplied by a flag, by the environment or by an earlier step
        is authoritative: re-asking it is how a wizard loses an answer the
        operator already gave on the command line.
        """
        existing = self.answers.get(key)
        if existing not in (None, ""):
            return str(existing)
        if self.yes:
            answer = default
        elif self.prompt is not None:
            answer = self.prompt(question, default)
        else:  # pragma: no cover - exercised only against a real terminal
            typed = input(f"    {question}" + (f" [{default}]: " if default else ": "))
            answer = typed.strip() or default
        answer = (answer or "").strip()
        self.answers[key] = answer
        return answer

    def confirm(self, key: str, question: str, *, default: bool = True) -> bool:
        """A yes/no question. ``--yes`` takes ``default`` and asks nothing."""
        suggested = "y" if default else "n"
        answer = self.ask(key, f"{question} [y/n]", suggested).lower()
        return answer.startswith("y")

    # -- output -----------------------------------------------------------

    def say(self, text: str = "") -> None:
        """Write one line of human text to the stream humans are reading."""
        target = self.stream
        if target is None:
            target = sys.stderr if self.json_mode else sys.stdout
        print(text, file=target)

    def detail(self, step_id: str, text: str) -> None:
        """Record what a step actually did, for the report and the JSON."""
        self.details[step_id] = text

    # -- http -------------------------------------------------------------

    def http(
        self,
        method: str,
        url: str,
        *,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        """One HTTP round trip that never raises and never blocks for long."""
        fetch = self.http_fetch or _default_fetch
        return fetch(method.upper(), url, body, timeout or DEFAULT_HTTP_TIMEOUT_S)

    # -- database ---------------------------------------------------------

    def db_config(self) -> dict[str, Any]:
        """The connection the wizard will use: answers first, settings behind.

        Answers win because the operator has just typed them; settings fill in
        everything they did not, so a box that already carries ``ROBOTHOR_DB_*``
        needs no questions at all.
        """
        database = self.settings.database
        answers = self.answers
        return {
            "host": str(answers.get("db_host") or database.host),
            "port": int(answers.get("db_port") or database.port),
            "dbname": str(answers.get("db_name") or database.name),
            "user": str(answers.get("db_user") or database.user),
            "password": str(answers.get("db_password") or database.password),
        }

    def db(self) -> Any:
        """One psycopg2 connection, short-timeout. Raises if it cannot connect.

        Raising is right here: every caller is inside a ``check()`` that turns
        the exception into a plan row, or an ``apply()`` the runner already
        catches. A connection helper that returned ``None`` would push a
        ``NoneType has no attribute cursor`` into the operator's terminal.
        """
        if self.db_factory is not None:
            return self.db_factory()
        import psycopg2

        config = self.db_config()
        return psycopg2.connect(
            **config, connect_timeout=int(self.settings.database.connect_timeout)
        )

    # -- settings the wizard writes ---------------------------------------

    @property
    def config_yaml_path(self) -> Path:
        """This instance's config.yaml -- under the workspace being created.

        Deliberately NOT ``settings.sources.config_yaml_path()``: that one
        follows ``ROBOTHOR_WORKSPACE``, and ``genus init --workspace /tmp/x``
        must write to the workspace it was told to create, not to the one this
        shell happens to be pointed at.
        """
        return self.workspace / ".robothor" / "config.yaml"

    def write_setting(self, env_name: str, value: Any) -> bool:
        """Store one declared setting in config.yaml. False in a dry run.

        Routed through the registry and ``settings.config_file.write_setting``,
        the same writer ``genus config set`` uses. Two implementations of
        "store a setting" would be two opinions about deprecated spellings and
        indentation, and a wizard that reports "applied" while the service
        reads something else.
        """
        if self.dry_run:
            return False
        from robothor.settings.config_file import write_setting
        from robothor.settings.registry import field_index

        record = field_index().get(env_name)
        if record is None:
            raise KeyError(f"{env_name} is not a declared setting")
        group, field = str(record["field"]).split(".", 1)
        path = self.config_yaml_path
        path.parent.mkdir(parents=True, exist_ok=True)
        write_setting(
            group,
            field,
            value,
            names=(record["env"], *record["aliases"]),
            path=path,
        )
        return True

    # -- resumable state --------------------------------------------------

    @property
    def state_path(self) -> Path:
        return self.workspace / ".robothor" / "init_state.yaml"

    @property
    def state(self) -> dict[str, str]:
        """``.robothor/init_state.yaml``, in the format earlier releases wrote.

        Loaded once. The file is a flat ``step: status`` mapping, and it stays
        one: an existing instance's state file has to keep loading, or every
        box that ran the old wizard restarts from step one.

        A file that is not that shape -- hand-edited, truncated, a list, a bare
        string -- reads as "no progress recorded" rather than raising. This is
        the one command whose job is to fix a broken box; a traceback out of
        the state loader takes that away.
        """
        if self._state is None:
            from robothor.setup import _load_init_state

            try:
                loaded = _load_init_state(self.workspace)
            except Exception:  # noqa: BLE001 - unreadable progress is no progress
                loaded = {}
            if not isinstance(loaded, dict):
                loaded = {}
            self._state = {
                str(key): str(value)
                for key, value in loaded.items()
                if isinstance(key, str) and isinstance(value, str)
            }
        return self._state

    def mark_completed(self, step_id: str) -> None:
        """Record a finished step, immediately, so a crash resumes after it."""
        if self.dry_run:
            return
        from robothor.setup import _save_init_state

        _save_init_state(self.workspace, step_id, "completed")
        self.state[step_id] = "completed"
