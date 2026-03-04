#!/bin/bash
# Email Classifier Task Creation Script

# Task 1: Fwd: Philip (escalate - unclear intent)
TASK1=$(curl -s -X POST http://localhost:9100/tasks -H "Content-Type: application/json" -d '{
  "title": "Review: Fwd email from Philip",
  "assignedToAgent": "supervisor",
  "priority": "normal",
  "tags": ["email", "escalation", "unknown-intent"],
  "body": "threadId: 19c6879e9126c65e\nFrom: Philip D'Agostino <philip@ironsail.ai>\nSubject: Fwd: Philip\nMessageCount: 4\nReason: Forwarded email with unclear intent, needs review"
}' | jq -r '.id')
echo "TASK1: $TASK1"

# Task 2: Revenue Strategy (analytical)
TASK2=$(curl -s -X POST http://localhost:9100/tasks -H "Content-Type: application/json" -d '{
  "title": "Review: Revenue Strategy & Monetization Roadmap from Daniel McCarthy",
  "assignedToAgent": "email-analyst",
  "priority": "high",
  "tags": ["email", "analytical", "revenue", "strategy"],
  "body": "threadId: 19c897ab34fae900\nFrom: Daniel McCarthy <daniel@valhallams.com>\nSubject: Revenue Strategy & Monetization Roadmap for Genius OS\nReason: Business strategy document - needs analysis and possible response"
}' | jq -r '.id')
echo "TASK2: $TASK2"

# Task 3: Calendar invite V/I Same Page
TASK3=$(curl -s -X POST http://localhost:9100/tasks -H "Content-Type: application/json" -d '{
  "title": "Calendar: V/I Same Page Meetings update from Joshua Quijano",
  "assignedToAgent": "email-responder",
  "priority": "normal",
  "tags": ["email", "calendar", "update-invitation"],
  "body": "threadId: 19c8ae43cf022c11\nFrom: Joshua Quijano <joshua@ironsail.ai>\nSubject: Updated invitation: V/I Same Page Meetings @ Wed Feb 25, 2026 12:30pm - 1pm (EST)\nAction: Review calendar invite update and respond if needed"
}' | jq -r '.id')
echo "TASK3: $TASK3"

# Task 4: Calendar invite Weekly Alignment
TASK4=$(curl -s -X POST http://localhost:9100/tasks -H "Content-Type: application/json" -d '{
  "title": "Calendar: Weekly Alignment Meetings update from Joshua Quijano",
  "assignedToAgent": "email-responder",
  "priority": "normal",
  "tags": ["email", "calendar", "update-invitation"],
  "body": "threadId: 19c8aecbd00a738e\nFrom: Joshua Quijano <joshua@ironsail.ai>\nSubject: Updated invitation: Weekly Alignment Meetings @ Mon Feb 23, 2026 10am - 10:30am (EST)\nAction: Review calendar invite update and respond if needed"
}' | jq -r '.id')
echo "TASK4: $TASK4"

# Task 5: Weekly Senior Leadership Standup
TASK5=$(curl -s -X POST http://localhost:9100/tasks -H "Content-Type: application/json" -d '{
  "title": "Meeting: Weekly Senior Leadership Standup from Daniel McCarthy",
  "assignedToAgent": "email-responder",
  "priority": "normal",
  "tags": ["email", "meeting", "leadership"],
  "body": "threadId: 19c8b019e28fbcf3\nFrom: Daniel McCarthy <daniel@valhallams.com>\nSubject: Weekly Senior Leadership Standup\nMessageCount: 7\nAction: Review meeting thread and respond if needed"
}' | jq -r '.id')
echo "TASK5: $TASK5"

# Task 6: Daily Standup Meeting update
TASK6=$(curl -s -X POST http://localhost:9100/tasks -H "Content-Type: application/json" -d '{
  "title": "Calendar: Daily Standup Meeting update from Rochelle Blaza",
  "assignedToAgent": "email-responder",
  "priority": "normal",
  "tags": ["email", "calendar", "update-invitation"],
  "body": "threadId: 19c8b7e27ac5c7c8\nFrom: Rochelle Blaza <rochelle@ironsail.ai>\nSubject: Updated invitation: Daily Standup Meeting @ Wed Feb 25, 2026 10:30am - 11am (EST)\nAction: Review calendar invite update and respond if needed"
}' | jq -r '.id')
echo "TASK6: $TASK6"

# Task 7: AI Session Reminder
TASK7=$(curl -s -X POST http://localhost:9100/tasks -H "Content-Type: application/json" -d '{
  "title": "Reminder: AI Session on Wednesday from Sandra Zelaya",
  "assignedToAgent": "email-responder",
  "priority": "normal",
  "tags": ["email", "reminder", "ai-session"],
  "body": "threadId: 19c8c17b193c6229\nFrom: Sandra Zelaya <sandra@ironsail.ai>\nSubject: Reminder: AI Session on Wednesday, February 25, 2026, at 9:30 AM ET\nAction: Acknowledge reminder, review details"
}' | jq -r '.id')
echo "TASK7: $TASK7"

# Task 8: Weekly Senior Leadership Standup invitation
TASK8=$(curl -s -X POST http://localhost:9100/tasks -H "Content-Type: application/json" -d '{
  "title": "Calendar: Weekly Senior Leadership Standup invitation from Daniel McCarthy",
  "assignedToAgent": "email-responder",
  "priority": "normal",
  "tags": ["email", "calendar", "invitation"],
  "body": "threadId: 19c8c582a3a292bf\nFrom: Daniel McCarthy <daniel@valhallams.com>\nSubject: Invitation: Weekly Senior Leadership Standup @ Tue Feb 24, 2026 12pm - 1pm (EST)\nAction: Respond to calendar invitation"
}' | jq -r '.id')
echo "TASK8: $TASK8"

# Task 9: Robothor Walkthrough invitation
TASK9=$(curl -s -X POST http://localhost:9100/tasks -H "Content-Type: application/json" -d '{
  "title": "Calendar: Robothor Walkthrough invitation from Philip D'Agostino",
  "assignedToAgent": "email-responder",
  "priority": "normal",
  "tags": ["email", "calendar", "invitation"],
  "body": "threadId: 19c8f8506b576268\nFrom: Philip D'Agostino <philip@ironsail.ai>\nSubject: Invitation: Robothor Walkthrough @ Fri Feb 27, 2026 12pm - 1pm (EST)\nAction: Respond to calendar invitation"
}' | jq -r '.id')
echo "TASK9: $TASK9"

# Task 10: AI Session Outline
TASK10=$(curl -s -X POST http://localhost:9100/tasks -H "Content-Type: application/json" -d '{
  "title": "Review: AI Session Outline from Sandra Zelaya",
  "assignedToAgent": "email-responder",
  "priority": "normal",
  "tags": ["email", "reply-needed", "ai-session"],
  "body": "threadId: 19c9004985eda341\nFrom: Sandra Zelaya <sandra@ironsail.ai>\nSubject: AI Session Outline for tomorrow\nAction: Review outline and respond with feedback if needed"
}' | jq -r '.id')
echo "TASK10: $TASK10"

# Save task IDs
echo "${TASK1} ${TASK2} ${TASK3} ${TASK4} ${TASK5} ${TASK6} ${TASK7} ${TASK8} ${TASK9} ${TASK10}" > /tmp/task_ids.txt
