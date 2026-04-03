---
name: diff
description: Generate and apply structured code changes to a file
tags: [editing, code]
tools_required: [read_file, write_file]
parameters:
  - name: file
    type: string
    description: Path to the file to modify
    required: true
  - name: changes
    type: string
    description: Description of desired changes
    required: true
output_format: text
---

# Diff — Structured File Editing

Read a file, plan precise changes based on a natural-language description, and apply them.

## Steps

1. **Read the file**: Use `read_file` to load the full contents of `{file}`. Note the total line count and language/format.

2. **Understand context**: Before making changes, understand the file's purpose, its imports/dependencies, and how the section you are modifying fits into the broader structure.

3. **Plan changes**: Based on the instruction (`{changes}`), identify the minimal set of edits needed. For each edit, note:
   - The line range affected
   - What the current code does
   - What the replacement code should do
   - Any side effects on other parts of the file (imports, type hints, tests)

4. **Generate a unified diff**: Produce the changes as a unified diff (--- a/file, +++ b/file, @@ line ranges @@). Include enough context lines (3+) for each hunk to make the diff unambiguous.

5. **Apply the changes**: Use `write_file` to write the modified file contents. The written file must be the complete, final version -- not a partial patch.

6. **Verify**: Re-read the file with `read_file` to confirm the write succeeded and the content matches expectations. Check that:
   - No syntax errors were introduced (matching brackets, quotes, indentation)
   - Import statements are still valid
   - The change matches the requested description

7. **Report**: Respond with:
   - The unified diff showing what changed
   - A brief summary of the modifications
   - Any warnings about potential side effects (e.g., callers that may need updating)

## Guidelines

- Make the smallest change that satisfies the request. Do not refactor unrelated code.
- Preserve the existing code style (indentation, quote style, naming conventions).
- If the requested change is ambiguous or could break functionality, explain the ambiguity before proceeding.
- Never silently delete code -- if removal is needed, call it out explicitly.
