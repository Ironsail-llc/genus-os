# Log Schemas — Notifier Field Specifications

## Core Principle

Every log entry has **notifier fields** that track processing state:
- `null` = needs initial processing
- `"pending_review"` or timestamp in `pendingReviewAt` = action taken, needs verification
- Timestamp in `reviewedAt` = verified complete

## Email Log Schema (`memory/email-log.json`)

```json
{
  // Identifiers (from system cron)
  "id": "string",
  "threadId": "string",
  "fetchedAt": "ISO timestamp",

  // Read state - null means agent needs to fetch full email
  "readAt": null | "ISO timestamp",
  
  // Filled AFTER reading (by heartbeat)
  "from": null | "string",
  "subject": null | "string",
  "snippet": null | "string (first 100 chars)",

  // Stage 1: Categorization (after reading)
  "categorizedAt": null | "ISO timestamp",
  "urgency": null | "low" | "medium" | "high" | "critical",
  "category": null | "work" | "personal" | "notification" | "spam",

  // Stage 2: Action
  "actionRequired": null | "respond" | "forward" | "escalate" | "note" | "none",
  "actionCompletedAt": null | "ISO timestamp",

  // Stage 3: Review
  "pendingReviewAt": null | "ISO timestamp (set when action completed)",
  "reviewedAt": null | "ISO timestamp (set after verification)"
}
```

## Calendar Log Schema (`memory/calendar-log.json`)

```json
{
  "id": "string",
  "title": "string",
  "start": "ISO timestamp",
  "end": "ISO timestamp",
  "location": "string | null",
  "description": "string | null",
  "attendees": ["string"],
  "conferenceUrl": "string | null",
  "fetchedAt": "ISO timestamp",

  // Stage 1: Categorization
  "categorizedAt": null | "ISO timestamp",
  "importance": null | "low" | "medium" | "high" | "critical",
  "category": null | "standup" | "meeting" | "external" | "personal",

  // Stage 2: Notification
  "notifyAt": null | "ISO timestamp (when to alert)",
  "notifiedAt": null | "ISO timestamp (when alert sent)",

  // Stage 3: Review
  "pendingReviewAt": null | "ISO timestamp",
  "reviewedAt": null | "ISO timestamp",

  // Change tracking
  "changeDetectedAt": null | "ISO timestamp",
  "changeType": null | "new" | "rescheduled" | "cancelled",
  "previousStart": null | "ISO timestamp",
  "previousEnd": null | "ISO timestamp"
}
```

## Task Log Schema (`memory/tasks.json`)

```json
{
  "id": "string",
  "title": "string",
  "description": "string | null",
  "source": "email" | "calendar" | "manual" | "jira",
  "sourceId": "string | null",
  "createdAt": "ISO timestamp",
  "dueAt": null | "ISO timestamp",

  // Stage 1: Categorization
  "categorizedAt": null | "ISO timestamp",
  "priority": null | "low" | "medium" | "high" | "critical",
  "category": null | "work" | "personal" | "admin",

  // Stage 2: Action
  "actionRequired": null | "string (what to do)",
  "actionCompletedAt": null | "ISO timestamp",
  "status": "pending" | "in_progress" | "completed" | "cancelled",

  // Stage 3: Review
  "pendingReviewAt": null | "ISO timestamp",
  "reviewedAt": null | "ISO timestamp"
}
```

## Jira Log Schema (`memory/jira-log.json`)

```json
{
  "key": "string (PROJ-123)",
  "summary": "string",
  "status": "string",
  "priority": "string",
  "assignee": "string | null",
  "reporter": "string",
  "created": "ISO timestamp",
  "updated": "ISO timestamp",
  "fetchedAt": "ISO timestamp",

  // Stage 1: Categorization
  "categorizedAt": null | "ISO timestamp",
  "relevance": null | "mine" | "team" | "watch" | "ignore",

  // Stage 2: Action
  "actionRequired": null | "review" | "update" | "close" | "none",
  "actionCompletedAt": null | "ISO timestamp",

  // Stage 3: Review
  "pendingReviewAt": null | "ISO timestamp",
  "reviewedAt": null | "ISO timestamp",

  // Change tracking
  "statusChangedAt": null | "ISO timestamp",
  "previousStatus": null | "string"
}
```

## Processing Rules

### System Cron (mechanical)
1. Fetch data from APIs
2. Write entries with ALL notifier fields set to `null`
3. Never modify non-null notifier fields

### Heartbeat Agent (AI judgment)
1. Find all entries with `null` fields → process them
2. Find all entries with `pendingReviewAt` set but `reviewedAt` null → verify them
3. After action: set `pendingReviewAt` = current timestamp
4. After verification: set `reviewedAt` = current timestamp

### Completion Criteria
Entry is complete when:
- All categorization fields have values
- `actionRequired` is set (even if "none")
- `actionCompletedAt` has timestamp (if action was needed)
- `pendingReviewAt` has timestamp
- `reviewedAt` has timestamp ← **final gate**
