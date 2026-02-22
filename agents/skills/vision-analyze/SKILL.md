---
name: vision-analyze
description: Capture and analyze a scene from the connected camera.
---

# Vision Analyze

Look through the connected camera and analyze the scene using vision AI.

## Inputs

- **prompt**: What to look for or analyze (optional, default: general scene description)
- **mode**: Analysis depth — "quick" for detection only, "full" for VLM scene analysis (optional, default: "quick")

## Execution

1. Capture a snapshot and run detection:
```
look(prompt="<PROMPT>")
```

2. If people are detected and identification is needed:
```
who_is_here()
```

3. Report findings with detected objects, recognized faces, and scene description.

## Rules

- Always describe what is visible before interpreting
- If unknown persons are detected, mention it but don't escalate unless asked
- Include confidence levels for object and face detections
- Respect privacy — don't store or share images without explicit instruction
