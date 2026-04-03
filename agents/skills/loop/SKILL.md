---
name: loop
description: Execute a task repeatedly on a configurable interval (polling pattern)
tags: [automation, monitoring]
tools_required: [wait_seconds]
parameters:
  - name: instruction
    type: string
    description: Task to execute each iteration
    required: true
  - name: interval_seconds
    type: integer
    description: Seconds between iterations
    default: 600
  - name: max_iterations
    type: integer
    description: Maximum number of iterations
    default: 6
output_format: json
---

# Loop / Polling Pattern

Execute a task on a recurring interval, collecting results from each iteration.

## Steps

1. **Initialize**: Set `iteration = 0` and `results = []`. Note the current time as `start_time`.

2. **Execute iteration**: Perform the task described by `{instruction}`. Record the output and whether it succeeded or failed.

3. **Record result**: Append to results:
   ```json
   {"iteration": <n>, "timestamp": "<ISO 8601>", "status": "ok|error", "output": "..."}
   ```

4. **Check exit conditions**: Stop early if:
   - The instruction output contains an explicit "STOP" or "DONE" signal
   - A critical error occurs that makes further iterations pointless
   - `iteration >= {max_iterations}`

5. **Wait**: If more iterations remain, call `wait_seconds` with `{interval_seconds}`. Then increment the iteration counter and go to step 2.

6. **Summarize**: After all iterations complete (or early exit), return:
   ```json
   {
     "iterations_completed": <int>,
     "max_iterations": {max_iterations},
     "interval_seconds": {interval_seconds},
     "early_exit": true|false,
     "early_exit_reason": "...",
     "results": [...]
   }
   ```

## Guidelines

- Keep each iteration's output concise -- avoid accumulating unbounded context.
- If the instruction involves checking a status, compare with the previous iteration and only report changes.
- Respect the agent's overall budget -- if running low on iterations or tokens, exit early and note the reason.
