"""The pr-reviewer's instance configuration, read from the ``pr_review`` settings group.

Every value is instance data (which repositories, which Chat space, which bot
login), so none of it has a platform default beyond "off". See
``ROBOTHOR_PR_REVIEW_*`` in docs/reference/configuration.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["ReviewerConfig", "load_config", "parse_repo_specs"]


def _csv(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").split(",") if v.strip())


def parse_repo_specs(
    repos: str, ticket_prefixes: str
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...], tuple[str, ...]]:
    """``(repos, per-repo prefixes, global prefixes)`` from the two settings.

    Both accept ``owner/repo:PREFIX`` entries — the format another review
    bot's ``ALLOWED_REPOS`` uses — so an instance can paste that list. A bare
    ``owner/repo`` in the repositories setting has no prefix of its own; a
    bare ``PREFIX`` in the prefixes setting applies to every repository that
    has none (the earlier format, still accepted).
    """
    out_repos: list[str] = []
    per_repo: list[tuple[str, str]] = []
    global_prefixes: list[str] = []
    for entry in _csv(repos):
        repo, _, prefix = entry.partition(":")
        repo = repo.strip()
        if repo and repo.lower() not in {r.lower() for r in out_repos}:
            out_repos.append(repo)
        if repo and prefix.strip():
            per_repo.append((repo, prefix.strip().upper()))
    for entry in _csv(ticket_prefixes):
        if ":" in entry:
            repo, _, prefix = entry.partition(":")
            if repo.strip() and prefix.strip():
                per_repo.append((repo.strip(), prefix.strip().upper()))
        else:
            global_prefixes.append(entry.upper())
    return tuple(out_repos), tuple(per_repo), tuple(global_prefixes)


@dataclass(frozen=True)
class ReviewerConfig:
    repos: tuple[str, ...] = ()
    watch_repos: bool = False
    bot_login: str = ""
    chat_space: str = ""
    chat_self_users: tuple[str, ...] = ()
    chat_lookback_minutes: int = 60
    claim_reaction: str = "\U0001f440"
    approved_reaction: str = "\U0001f44d"
    telegram_digest: bool = False
    require_ticket: bool = False
    #: Prefixes for repositories without their own (bare ``PREFIX`` entries).
    ticket_prefixes: tuple[str, ...] = ()
    #: ``(owner/repo, PREFIX)`` pairs from ``owner/repo:PREFIX`` entries.
    repo_ticket_prefixes: tuple[tuple[str, str], ...] = ()
    blocking_event: str = "REQUEST_CHANGES"
    max_concurrent: int = 2
    agent_id: str = "pr-reviewer"
    clone_root: str = ""
    review_model: str = ""
    review_budget_usd: float = 25.0
    review_effort: str = "high"
    review_max_turns: int = 80
    review_round_timeout_s: float = 1800.0
    guidelines_path: str = ""
    stale_after_minutes: int = 180
    skip_labels: tuple[str, ...] = ()
    retry_cooldown_minutes: int = 60
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.repos or self.bot_login or self.chat_space)

    def allows_repo(self, repo: str) -> bool:
        return repo.lower() in {r.lower() for r in self.repos}

    def prefixes_for(self, repo: str) -> tuple[str, ...]:
        """The ticket prefixes for ``repo``: its own, else the global ones."""
        own = tuple(p for r, p in self.repo_ticket_prefixes if r.lower() == repo.lower())
        return own or self.ticket_prefixes

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
    repos, per_repo, global_prefixes = parse_repo_specs(s.repos, s.ticket_prefixes)
    effort = s.review_effort.strip().lower()
    return ReviewerConfig(
        repos=repos,
        watch_repos=bool(s.watch_repos),
        bot_login=s.bot_login.strip(),
        chat_space=s.chat_space.strip(),
        chat_self_users=_csv(s.chat_self_users),
        chat_lookback_minutes=max(1, int(s.chat_lookback_minutes)),
        claim_reaction=s.claim_reaction.strip(),
        telegram_digest=bool(s.telegram_digest),
        require_ticket=bool(s.require_ticket),
        ticket_prefixes=global_prefixes,
        repo_ticket_prefixes=per_repo,
        blocking_event=s.blocking_event.strip().upper() or "REQUEST_CHANGES",
        max_concurrent=max(1, int(s.max_concurrent)),
        agent_id=s.agent_id.strip() or "pr-reviewer",
        clone_root=clone_root,
        review_model=s.review_model.strip(),
        review_budget_usd=float(s.review_budget_usd),
        review_effort=effort,
        review_max_turns=max(1, int(s.review_max_turns)),
        review_round_timeout_s=max(60.0, float(s.review_round_timeout_s)),
        guidelines_path=s.guidelines_path.strip(),
        approved_reaction=s.approved_reaction.strip(),
        stale_after_minutes=max(10, int(s.stale_after_minutes)),
        skip_labels=_csv(s.skip_labels),
        retry_cooldown_minutes=max(1, int(s.retry_cooldown_minutes)),
    )
