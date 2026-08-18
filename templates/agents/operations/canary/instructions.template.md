# Canary Agent — Error Recovery Validation

You are a test agent designed to validate the error recovery pipeline.

## Task

1. Try to read a file that does not exist: `/tmp/robothor-canary-nonexistent.txt`
2. When that fails, note the error type and recovery action
3. Try to write a test file: `/tmp/robothor-canary-output.txt` with content "Canary test passed"
4. Read it back to verify
5. Report what happened at each step, including any error recovery actions that fired

## Expected Behavior

- Step 1 should fail with NOT_FOUND error
- Error recovery should either inject guidance or spawn a helper
- Steps 3-4 should succeed
- Final output should summarize the recovery chain
