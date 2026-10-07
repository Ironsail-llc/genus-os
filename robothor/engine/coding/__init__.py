"""Robothor drives the Claude Code CLI to verified completion.

A cheap orchestrator model hands a coding task to ``claude -p`` running in a
dedicated git worktree, then judges the result itself: it runs the acceptance
command, checks that a commit exists, and resumes the same Claude Code session
with the failure output until the check passes or the round budget is spent.

* :mod:`.runner` — argv, the stream-json parser, the subprocess.
* :mod:`.env` — the allowlisted child environment and the token.
* :mod:`.worktree` — one git worktree per job.
* :mod:`.jobs` — durable jobs, the completion loop, resume-on-restart.

The tools that drive it are in ``robothor/engine/tools/handlers/claude_code.py``.
"""
