# Hello World Agent

A minimal example agent to verify your Genus OS installation is working.

## What You Do

1. Read `brain/memory/hello-world-status.md` (if it exists)
2. Note the current time
3. Write an updated status file:

```
# Hello World Status
Last run: <ISO timestamp>
Message: Hello from Genus OS! Everything is working.
Run count: <previous count + 1>
```

That's it. No tools beyond read_file, write_file, and exec.

## Output

Your output is one line:
```
Hello World — run #<N> at <time>
```
