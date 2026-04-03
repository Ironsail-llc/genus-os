---
name: batch
description: Run a prompt or instruction across multiple files matching a glob pattern
tags: [automation, bulk]
tools_required: [spawn_agents, list_directory, read_file]
parameters:
  - name: glob
    type: file_glob
    description: File pattern to process (e.g., "src/**/*.py")
    required: true
  - name: instruction
    type: string
    description: What to do with each file
    required: true
  - name: concurrency
    type: integer
    description: Max concurrent sub-agents
    default: 3
output_format: json
composable: false
---

# Batch Processing

Run an instruction against every file matching a glob pattern, using sub-agents for parallel execution.

## Steps

1. **Expand the glob**: Use `list_directory` to walk the directory tree implied by `{glob}`. Collect the full list of matching file paths. If zero files match, return immediately with `{"files_processed": 0, "error": "no files matched pattern"}`.

2. **Preview**: Log the total count of matched files. If more than 50 files match, warn the user and ask for confirmation before proceeding (unless running autonomously).

3. **Read and validate**: For each file, use `read_file` to confirm it exists and is readable. Skip binary or unreadable files and note them in a `skipped` list.

4. **Spawn sub-agents**: Use `spawn_agents` to fan out processing. Pass each sub-agent:
   - The file path and its contents
   - The instruction: `{instruction}`
   - A unique task ID derived from the file path
   Limit concurrency to `{concurrency}` simultaneous agents.

5. **Collect results**: As each sub-agent completes, capture its output. Track successes, failures, and any errors with the originating file path.

6. **Summarize**: Return a JSON object:
   ```json
   {
     "files_processed": <int>,
     "succeeded": [{"file": "...", "result": "..."}],
     "failed": [{"file": "...", "error": "..."}],
     "skipped": ["..."]
   }
   ```

## Error Handling

- If a sub-agent fails, record the error and continue with remaining files.
- If more than half the files fail, include a `warning` field in the output suggesting the instruction may need adjustment.
- Never abort the entire batch on a single-file failure.
