"""The webchat half: N turns over HTTP against a running engine's ``/chat/send``.

Each turn gets its own session key, so every turn starts from the same empty
history and the numbers stay comparable from turn to turn and run to run.
"""

from __future__ import annotations

import subprocess
import time
import uuid
from typing import TYPE_CHECKING, Any

from bench.turn.dbstats import open_counter
from bench.turn.sse import parse_sse, turn_timings
from bench.turn.steps import step_metrics

if TYPE_CHECKING:
    from collections.abc import Callable

STEPS_SQL = (
    "SELECT step_type, tool_name, duration_ms, completed_at "
    "FROM agent_run_steps WHERE run_id = %s ORDER BY created_at, step_number"
)


def measure_ready(
    engine_url: str,
    restart_cmd: str,
    *,
    timeout_s: float = 300.0,
    get: Callable[[str], int] | None = None,
) -> float | None:
    """Run ``restart_cmd`` and time it until ``/ready`` answers 200."""
    import httpx

    def default_get(url: str) -> int:
        try:
            return httpx.get(url, timeout=2.0).status_code
        except httpx.HTTPError:
            return 0

    probe = get or default_get
    start = time.perf_counter()
    subprocess.run(restart_cmd, shell=True, check=True)  # noqa: S602 - operator-supplied command
    # A restart that returns before the old process stops would answer 200
    # from the old one; wait for /ready to drop first, briefly.
    drop_deadline = time.perf_counter() + 10.0
    while time.perf_counter() < drop_deadline and probe(f"{engine_url}/ready") == 200:
        time.sleep(0.1)
    deadline = start + timeout_s
    while time.perf_counter() < deadline:
        if probe(f"{engine_url}/ready") == 200:
            return (time.perf_counter() - start) * 1000.0
        time.sleep(0.2)
    return None


def _run_steps(dsn: str, run_id: str, wait_s: float = 5.0) -> list[dict[str, Any]]:
    """The run's steps, once the background persist has written them."""
    if not dsn or not run_id:
        return []
    try:
        import psycopg2
    except ImportError:
        return []
    deadline = time.perf_counter() + wait_s
    rows: list[tuple[Any, ...]] = []
    try:
        conn = psycopg2.connect(dsn)
    except Exception:  # noqa: BLE001 - steps are optional
        return []
    try:
        conn.autocommit = True
        while True:
            with conn.cursor() as cur:
                cur.execute(STEPS_SQL, (run_id,))
                rows = cur.fetchall()
            if any(r[0] == "llm_call" for r in rows) or time.perf_counter() > deadline:
                break
            time.sleep(0.2)
    finally:
        conn.close()
    return [
        {"step_type": r[0], "tool_name": r[1], "duration_ms": r[2], "completed_at": r[3]}
        for r in rows
    ]


def run_http(
    *,
    engine_url: str,
    token: str,
    runs: int,
    agent: str,
    message: str,
    db_url: str,
    settle_ms: int,
    restart_cmd: str = "",
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    import httpx

    counter, notes = open_counter(db_url, log)
    turns: list[dict[str, Any]] = []
    meta: list[dict[str, Any]] = []
    ready: float | None = None
    if restart_cmd:
        ready = measure_ready(engine_url, restart_cmd)
        log(f"engine ready in {ready:.0f} ms" if ready is not None else "engine never became ready")

    bench_id = uuid.uuid4().hex[:8]
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=engine_url, headers=headers, timeout=httpx.Timeout(300.0)) as client:
        for i in range(runs):
            body = {"session_key": f"agent:{agent}:bench-{bench_id}-{i}", "message": message}
            before = counter.snapshot()
            t0 = time.perf_counter()
            lines: list[tuple[float, str]] = []
            with client.stream("POST", "/chat/send", json=body) as response:
                if response.status_code != 200:
                    response.read()
                    raise RuntimeError(
                        f"/chat/send answered {response.status_code}: {response.text[:300]}"
                    )
                # The generator is consumed line by line as the stream
                # arrives, so each line carries its own arrival time.
                lines.extend((time.perf_counter(), line) for line in response.iter_lines())
            timing = turn_timings(parse_sse(lines), t0)
            time.sleep(settle_ms / 1000.0)
            after = counter.snapshot()
            turn: dict[str, Any] = {
                "ttft_ms": timing.ttft_ms,
                "done_ms": timing.done_ms,
                "finalize_tail_ms": timing.finalize_tail_ms,
                "db_statements": counter.delta(before, after),
            }
            steps = step_metrics(_run_steps(db_url, timing.run_id or ""))
            turn["setup_total_ms"] = steps["setup_total_ms"]
            turn["tool_overhead_ms"] = steps["tool_overhead_ms"]
            turn["tool_ms"] = steps["tool_ms"]
            turns.append(turn)
            meta.append(
                {"turn": i, "run_id": timing.run_id, "status": timing.status, "error": timing.error}
            )
            log(
                f"http turn {i + 1}/{runs}: done {timing.done_ms or 0:.0f} ms, "
                f"ttft {timing.ttft_ms or 0:.0f} ms, status {timing.status or timing.error}"
            )
    counter.close()
    return {"turns": turns, "meta": meta, "notes": notes, "engine_ready_ms": ready}
