# Thermal envelope — GB10 (ThinkStation PGX class)

Why this file exists: the firmware exposes no ACPI trip points ("[Firmware Bug]: No valid
trip points!") and the EC hard-cuts power near ~95C with no warning. Every threshold in
`scripts/thermal-guard.sh`, `robothor/engine/thermal_pressure.py` and the local inference
gate is derived from the measurements below. They were measured, not guessed.

## Measured 2026-08-28

Conditions: agent engine stopped, orchestrator stopped, `ollama` serving only the probe.
`OLLAMA_NUM_PARALLEL=2`, CPU frequency capped at the standing 65%. Temperature is the max
across all `/sys/class/thermal/thermal_zone*/temp` — the same reading both guards use.

| Load | GPU power | Package temp | Notes |
|---|---|---|---|
| Idle, no model resident | 5-13 W | 49-58 C | floor |
| 1x 27B, 27,010-token prompt | 62 W | 47 -> 84 C in 58 s | plateaued, did not run away |
| 1x 27B, 7,510-token prompt | ~62 W | 59 -> 81 C in 11 s | +22 C from one ordinary request |
| 2nd such request, back-to-back | ~62 W | 81 -> 85 C in 8 s | crosses THROTTLE_C |
| Incident 2026-08-28 09:25 (2 slots + embed/rerank) | 74 W | **96 C** | triggered the 94 C clean reboot |

Recovery: effectively instantaneous. 85 C -> 62 C and 84 C -> 58 C both inside a few seconds
of the GPU going idle.

## The model that follows

Package temperature tracks **instantaneous GPU power**, not accumulated heat soak. Thermal
mass is negligible: it heats at roughly 2 C/s under 27B prefill and sheds just as fast.

Across the three load points the relationship is linear and consistent:

```
T_package  ~=  55 C  +  0.65 * (GPU_watts - 13)
```

(85-55)/(62-13) = 0.61 C/W, and from the incident (96-55)/(74-13) = 0.67 C/W.

Rearranged, this is the design constraint:

| Ceiling | Sustained GPU budget |
|---|---|
| 80 C | ~51 W |
| 85 C (THROTTLE_C) | ~59 W |
| 90 C (WARN_C) | ~67 W |
| 94 C (CRIT_C, clean reboot) | ~73 W |

## Correction: the budget is shared with the CPU

The table above varies GPU load with the CPU near idle, and it is incomplete. On
2026-08-28 at 10:28 the box reached **96 C and clean-rebooted** while serving ONE
27B stream (`OLLAMA_NUM_PARALLEL=1`, gate slots=1) — a configuration measured at
~85 C above — because the full test suite was saturating the CPU at the same time.

So the package budget is shared, and a GPU-only model under-predicts:

```
T_package  ~=  55 C  +  0.65 * (GPU_watts - 13)  +  <CPU term>
```

Two consequences:

1. **Shed rungs cannot be set from the GPU-only peak.** They must leave room for
   concurrent CPU load, which is why stage 1 sits at 82 C rather than 87 C.
2. **Never run the full test suite while ollama is serving.** The `-m "not slow
   and not llm and not e2e"` filter does NOT exclude tests that reach a live local
   model; with ollama up they issue real 27B requests (29 of them in three minutes,
   one running 61 s). Stop ollama first.

## The fix that actually bounds it: cap the GPU clock

GB10 reports **no GPU power limit** (`nvidia-smi -q -d POWER` returns N/A for every
limit field), so clock is the only hardware lever. Measured with qwen3.8:27b, six
back-to-back 7.5k-token requests:

| Clock cap | Single request | Sustained plateau | Power | Generation |
|---|---|---|---|---|
| 1000 MHz | 72 C | — | 20 W | 18.2 tok/s |
| **1500 MHz** | 70 C | **76 C** | 28 W | 24.0 tok/s |
| 2000 MHz | 75 C | 81 C | 39 W | 28.3 tok/s |
| uncapped (3003 MHz) | 86 C | — | 69 W | 26.6 tok/s |

**The uncapped row is slower than 2000 MHz while running 11 C hotter.** Above
~2000 MHz this part burns watts fighting its own thermal wall, so capping is a
correction rather than a sacrifice.

`robothor-gpu-clock-cap.service` pins 1500 MHz at boot: a 76 C sustained plateau
leaves 18 C to the 94 C clean reboot and 6 C below thermal-shed stage 1, for ~10%
less generation throughput than uncapped. This is the control that makes the box
safe; the gate and the guards are what handle everything the cap cannot predict.

## What this means for the design

1. **The control variable is GPU power, and concurrency is how we spend it.** One 27B stream
   costs ~62 W and plateaus near 85 C — survivable but with no margin. A second parallel
   stream plus concurrent embedding and reranking is what reached 96 C.
2. **Concurrency capping alone is not sufficient.** A *single* stream reaches 81 C from idle
   in 11 seconds, and two ordinary requests back-to-back cross THROTTLE_C. There is no
   concurrency setting at which continuous 27B work is safe indefinitely.
3. **Pacing is sufficient, and it is cheap.** Because recovery is near-instantaneous, a brief
   gap between requests restores nearly all headroom. The gate should pace on temperature,
   not only limit parallelism.
4. **The 27B is viable on this box** — at roughly one stream at a time, with the aux traffic
   (embeddings, reranker) gated against it rather than running alongside it.

## Reproducing

`ollama` running, engine stopped. Build a payload and fire it while sampling:

```bash
python3 -c 'import json;p="word "*7000;print(json.dumps({"model":"qwen3.8:27b","stream":False,
  "options":{"num_predict":200},"messages":[{"role":"user","content":p}]}))' > /tmp/p.json
curl -s -X POST http://127.0.0.1:11434/api/chat -H 'Content-Type: application/json' -d @/tmp/p.json
```

The prompt must go through `-d @file`; a 27k-token prompt on the command line exceeds ARG_MAX.

**Safety when probing:** stop `ollama` before running the test suite, and abort at
86 C when driving load deliberately. It heats ~2 C/s, so a 2 s sampling interval can
overshoot by several degrees; sample every 1 s above 80 C. Never leave a probe unattended —
`robothor-thermal-guard` reboots the box at 94 C and the firmware cuts power at ~95 C.
Unload models afterwards with `keep_alive: 0` and confirm `/api/ps` is empty.
