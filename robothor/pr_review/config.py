"""The pr-reviewer's instance configuration, read from the ``pr_review`` settings group.

Every value is instance data (which repositories, which Chat space, which bot
login), so none of it has a platform default beyond "off". See
``ROBOTHOR_PR_REVIEW_*`` in docs/reference/configuration.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["ReviewerConfig", "load_config"]


def _csv(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").split(",") if v.strip())


@dataclass(frozen=True)
class ReviewerConfig:
    repos: tuple[str, ...] = ()
    watch_repos: bool = False
    bot_login: str = ""
    chat_space: str = ""
    chat_self_users: tuple[str, ...] = ()
    chat_lookback_minutes: int = 60
    claim_reaction: str = "\U0001f440"
    telegram_digest: bool = False
    require_ticket: bool = False
    ticket_prefixes: tuple[str, ...] = ()
    blocking_event: str = "REQUEST_CHANGES"
    max_concurrent: int = 2
    agent_id: str = "pr-reviewer"
    clone_root: str = ""
    review_model: str = ""
    review_budget_usd: float = 3.0
    stale_after_minutes: int = 180
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.repos or self.bot_login or self.chat_space)

    def allows_repo(self, repo: str) -> bool:
        return repo.lower() in {r.lower() for r in self.repos}

    def clone_dir(self, repo: str) -> Path:
        root = self.clone_root or str(Path.home() / "robothor" / ".genus" / "pr-review" / "repos")
        owner, name = repo.split("/", 1)
        return Path(root).expanduser() / owner / name


def load_config() -> ReviewerConfig:
    from robothor.settings import get_settings

    settings = get_settings()
    s = settings.pr_review
    clone_root = s.clone_root.strip()
    if not clone_root:
        workspace = settings.paths.workspace.strip()
        base = Path(workspace).expanduser() if workspace else Path.home() / "robothor"
        clone_root = str(base / ".genus" / "pr-review" / "repos")
    return ReviewerConfig(
        repos=_csv(s.repos),
        watch_repos=bool(s.watch_repos),
        bot_login=s.bot_login.strip(),
        chat_space=s.chat_space.strip(),
        chat_self_users=_csv(s.chat_self_users),
        chat_lookback_minutes=max(1, int(s.chat_lookback_minutes)),
        claim_reaction=s.claim_reaction.strip(),
        telegram_digest=bool(s.telegram_digest),
        require_ticket=bool(s.require_ticket),
        ticket_prefixes=_csv(s.ticket_prefixes),
        blocking_event=s.blocking_event.strip().upper() or "REQUEST_CHANGES",
        max_concurrent=max(1, int(s.max_concurrent)),
        agent_id=s.agent_id.strip() or "pr-reviewer",
        clone_root=clone_root,
        review_model=s.review_model.strip(),
        review_budget_usd=float(s.review_budget_usd),
        stale_after_minutes=max(10, int(s.stale_after_minutes)),
    )
