# bench/turn: per-turn latency

One command measures a whole chat turn end to end, on a running stack with the
mock model, and compares the numbers with a committed baseline.

```bash
python -m bench.turn --engine-url http://127.0.0.1:18800 --db-url "$DB_URL" --runs 20
```

Two halves, chosen with `--half http|inprocess|both` (default `both`):

| Half | What it drives | Where it runs |
|------|----------------|---------------|
| `http` | N webchat turns: `POST /chat/send` on the engine, the SSE stream read line by line as it arrives. Each turn uses its own session key, so every turn starts from an empty history. | Anywhere that reaches the engine's port. Needs an engine token. |
| `inprocess` | N Telegram-shaped turns: `runner.execute(trigger_type=TELEGRAM)` with no `on_content`, so the model call does not stream, as the Telegram channel calls it. | Wherever the engine's environment is set (`ROBOTHOR_WORKSPACE`, `ROBOTHOR_DB_*`, the model base URL): inside the engine container, or CI. |

## What it records, per turn

| Metric | Half | From |
|--------|------|------|
| `setup_ms` | inprocess | The runner's `SETUP <n>ms` log line (`robothor/engine/runner.py`), captured by a logging handler. |
| `setup_total_ms` | both | The `tools_built` warmup step: setup plus building the toolset. |
| `ttft_ms` | http | Request sent to the first `delta` event. |
| `first_llm_ms` | inprocess | `execute` called to the first model call returning (a non-streaming turn has no first token). |
| `done_ms` | both | Request sent to the `done` event (http), or `execute` returning (inprocess). |
| `finalize_tail_ms` | both | `done` minus the last `delta` (http), or `execute` returning minus the last model call returning (inprocess). |
| `tool_ms` | both | Summed duration of the turn's tool-call steps: the handlers' own time. |
| `tool_overhead_ms` | both | For each gap between two model calls with tool calls in it, the gap minus the tools' own time: what the loop spends around a tool. |
| `db_statements` | both | `pg_stat_statements` calls in the engine's database, before the turn and after it plus `--settle-ms` (default 500 ms, so the run's background persist is counted). The bench's own reads of the view are left out; other activity on the database in that window is not, so run it on a quiet stack. |
| `prompt_tokens` | inprocess | The first model call's messages and tool schemas, at four characters a token. A stable number to compare, not an exact count. |
| `tool_schemas` | inprocess | How many tool schemas the first model call carried. |
| `engine_ready_ms` | http | With `--restart-cmd "<command>"`: the command run, then `/ready` polled until it answers 200. Measured once per bench run. |

Steps (`setup_total_ms`, `tool_*`) come from `agent_run_steps` by the `run_id`
in the `done` event (http) or from the returned run (inprocess). Without
`--db-url` the http half has no steps and no statement counts; the run still
completes and the JSON says why under `notes`.

### pg_stat_statements

The bench runs `CREATE EXTENSION IF NOT EXISTS pg_stat_statements`. The view
only works when Postgres preloads the library, which the compose stack does not
do by default. Without it `db_statements` is off, with the reason printed and
kept in the result. To turn it on for a local stack, start Postgres with
`-c shared_preload_libraries=pg_stat_statements` (a compose override adding
`command: ["postgres", "-c", "shared_preload_libraries=pg_stat_statements"]` to
the `postgres` service), then restart it.

## Output and baselines

Each half first runs `--warmup` turns (default 1, the cold first turn, which
on CI takes ten times as long as the rest) and leaves them out of the summary;
they are kept under `warmup` in the JSON. Every run then prints p50 and p95 per metric (linear interpolation; turns that did
not record a metric are left out of it) and, with `--out FILE`, writes the
JSON result: the summary, every turn's numbers, each turn's `run_id` and
status, and `notes`. With `--half both`, `--out` and `--baseline` get `-http`
and `-inprocess` inserted before the extension.

The result is compared with the committed set: `bench/turn/baselines/<half>.json`
by default, `bench/turn/baselines/ci-<half>.json` with `--baseline-set ci` (what
CI uses), or one file named with `--baseline FILE`. A named file that is
missing or is the empty sentinel exits 2, so a mistyped path cannot pass.

The threshold depends on the metric:

| Metric | Regression when the p50 or p95 is |
|--------|-----------------------------------|
| Exact: `prompt_tokens`, `tool_schemas` | above the baseline at all (0%). The same code gives the same number every run, so one more tool schema is a regression. A decrease passes and prints a hint to re-record the baseline, so the gain is locked in. |
| `db_statements` | more than `--threshold` (default 10%) over the baseline, with no absolute floor. Background persists move its p95 by a few statements from run to run (80 and 84 on the same code). |
| Timings: every `*_ms` metric | more than `--timing-threshold` over the baseline **and** more than `--min-delta` (default 5 ms) over it in absolute terms. `--timing-threshold` defaults to 30% with `--baseline-set ci` and to `--threshold` otherwise. |

The two looser timing defaults come from measurements. Three CI runs of the
same code put `done_ms` p50 at 30.6, 40.3 and 44.0 ms, a spread wider than 10%.
A clean local re-run moved `finalize_tail_ms` p95 from 2.3 to 5.2 ms, which is
noise under a 5 ms floor. A metric missing on either side is skipped and
listed. Exit codes: 0 within threshold, 1 regression, 2 the benchmark could
not run or a turn did not complete (timings of a failed run measure the
failure, so they are neither compared nor recorded).

The committed baselines start as an empty sentinel (`"empty": true`). Against
one picked from the committed set (not named with `--baseline`), comparison is
a no-op that prints `no baseline recorded`. Record a real
baseline on the machine it will be compared on, and commit the file:

| File | Recorded on | Compared by |
|------|-------------|-------------|
| `baselines/http.json`, `baselines/inprocess.json` | The local stack with the mock model | You, before and after an engine change |
| `baselines/ci-inprocess.json` | The `bench-turn` job in `.github/workflows/ci.yml` (download its `bench-turn` artifact) | That job, on every pull request touching `robothor/engine/` |
| `baselines/ci-http.json` | The compose job in `.github/workflows/install-gate.yml` | That step, nightly, report only |

`--compare RESULT.json` compares a saved result without driving anything, and
with `--update-baseline` turns a saved result into the baseline.

**Re-recording a CI baseline.** One run is not a baseline: runners vary. Record
it from at least three dispatched runs and keep the median of each metric:

```bash
gh workflow run ci.yml --ref <branch>       # three times, one after another
gh run download <run-id> -n bench-turn -D run-1   # likewise run-2, run-3
python -m bench.turn --merge-baselines run-1/bench-turn-inprocess.json \
  run-2/bench-turn-inprocess.json run-3/bench-turn-inprocess.json \
  --out bench/turn/baselines/ci-inprocess.json
```

`--merge-baselines` refuses fewer than three files or files from different
halves. It writes the median p50 and p95 per metric, and lists the merged runs
under `merged_from`. Dispatched runs on one ref queue behind each other, and a
third dispatch replaces a queued second, so start each one after the previous
run has begun.

## On the local stack

With the stack up on the mock model (engine on 18800, Postgres on 55432):

```bash
# A one-hour owner token, minted inside the engine (which holds the signing key).
TOKEN="$(docker exec genus-dev-engine python -c 'from robothor.auth.tokens import issue_access_token; from robothor.engine.config import EngineConfig; print(issue_access_token("bench-operator", EngineConfig.from_env().tenant_id, "owner", audience="genus-engine", scopes=("engine:*",), ttl_seconds=3600))')"

# Webchat half, from the checkout, plus the restart-to-ready time.
python -m bench.turn --half http --runs 20 --token "$TOKEN" \
  --engine-url http://127.0.0.1:18800 \
  --db-url "postgresql://<user>:<password>@127.0.0.1:55432/<database>" \
  --restart-cmd "docker restart genus-dev-engine" --out /tmp/bench-turn.json

# Telegram-shaped half, inside the engine (bench/ is not in the image).
docker cp bench genus-dev-engine:/app/bench
docker exec -w /app genus-dev-engine python -m bench.turn --half inprocess --runs 20 \
  --out /tmp/bench-turn.json
docker cp genus-dev-engine:/tmp/bench-turn.json /tmp/bench-turn-inprocess.json
```

Inside the engine `--db-url` defaults to the `ROBOTHOR_DB_*` environment, and
`--mint-token` mints the token itself. Record the baselines with
`--update-baseline` on the same commands (for the in-process half, record it in
the container and copy `bench/turn/baselines/inprocess.json` back out), then
commit them.

## CI

The `bench-turn` job in `.github/workflows/ci.yml` runs the unit tests in
`bench/turn/tests/` on every push. On a pull request that touches
`robothor/engine/` (or a manual dispatch) it also runs the in-process half:
20 turns against pgvector Postgres started with `pg_stat_statements` preloaded,
Redis and the mock model (`tests/acceptance/mock_llm`), compared with
`baselines/ci-inprocess.json`, and uploads the result as the `bench-turn`
artifact. A whole stack per pull request is too heavy, so the http half runs
nightly in the install gate's compose job, report only, and uploads
`bench-turn-http`.
