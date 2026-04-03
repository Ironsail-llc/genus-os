---
name: code-review
description: Structured code quality assessment with severity ratings
tags: [quality, review]
tools_required: [read_file, list_directory]
parameters:
  - name: target
    type: string
    description: File or directory to review
    required: true
  - name: focus
    type: string
    description: Review focus area (security, performance, readability, all)
    default: all
output_format: json
---

# Code Review

Perform a structured code quality review of a file or directory, producing actionable findings with severity ratings.

## Steps

1. **Discover files**: If `{target}` is a directory, use `list_directory` to enumerate source files (skip binary, vendored, and generated files). If it is a single file, proceed directly.

2. **Read and analyze**: For each file, use `read_file` to load the contents. Analyze based on the focus area (`{focus}`):
   - **security**: Look for injection risks, hardcoded secrets, unsafe deserialization, missing input validation, path traversal, SSRF, unescaped output.
   - **performance**: Identify N+1 queries, unbounded loops, missing caching opportunities, excessive allocations, blocking calls in async code.
   - **readability**: Check naming conventions, function length, dead code, missing docstrings, inconsistent style, overly complex conditionals.
   - **all**: Apply all of the above categories.

3. **Rate each finding**: Assign a severity:
   - `critical` -- security vulnerability or data loss risk, must fix before merge
   - `high` -- bug or significant performance issue
   - `medium` -- code smell or maintainability concern
   - `low` -- style nit or minor improvement suggestion

4. **Produce findings**: For each issue, record the file path, line number(s) or function name, category, severity, a one-line summary, and a suggested fix.

5. **Summarize**: Return a JSON object:
   ```json
   {
     "target": "{target}",
     "focus": "{focus}",
     "files_reviewed": <int>,
     "findings": [
       {
         "file": "...",
         "line": "...",
         "category": "security|performance|readability",
         "severity": "critical|high|medium|low",
         "summary": "...",
         "suggestion": "..."
       }
     ],
     "summary": "Brief overall assessment"
   }
   ```

## Guidelines

- Be specific: cite line numbers and function names, not vague observations.
- Limit findings to genuinely actionable items -- skip style preferences that are already consistent within the codebase.
- If no issues are found, return an empty findings array with a positive summary.
