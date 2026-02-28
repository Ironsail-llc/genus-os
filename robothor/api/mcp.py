"""
MCP Server for Robothor Memory System.

Provides a Model Context Protocol (MCP) interface so external models
(Claude Code, Cursor, etc.) can access the memory system. Runs locally
with stdio transport.

Architecture:
    MCP Client -> stdio -> this server -> robothor.* modules -> PostgreSQL

Tools (57):
    - Memory: search_memory, store_memory, get_stats, get_entity
    - Vision: look, who_is_here, enroll_face, set_vision_mode
    - Memory blocks: memory_block_read, memory_block_write, memory_block_list, append_to_block
    - CRM interaction: log_interaction
    - CRM People: create_person, get_person, update_person, list_people, delete_person
    - CRM Companies: create_company, get_company, update_company, list_companies, delete_company
    - CRM Notes: create_note, get_note, list_notes, update_note, delete_note
    - CRM Tasks: create_task, get_task, list_tasks, update_task, delete_task,
      resolve_task, list_agent_tasks, approve_task, reject_task
    - CRM Notifications: send_notification, get_inbox, ack_notification
    - CRM Metadata: get_metadata_objects, get_object_metadata, search_records
    - CRM Conversations: list_conversations, get_conversation,
      list_messages, create_message, toggle_conversation_status
    - Impetus One: search_patients, get_patient_details, get_patient_clinical_notes,
      get_patient_prescriptions, search_prescriptions, get_prescription_status,
      search_medications, search_pharmacies, get_appointments, list_actable_providers,
      create_prescription_draft, schedule_appointment, transmit_prescription

Start:
    python -m robothor.api.mcp
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

# ─── Service URL Resolution ──────────────────────────────────────────

BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://localhost:9100")
VISION_URL = os.environ.get("VISION_SERVICE_URL", "http://localhost:8600")


def _svc_url(service: str, path: str = "") -> str:
    """Resolve a service URL with optional service registry fallback."""
    try:
        from robothor.services.registry import get_service_url

        url = get_service_url(service, path)
        if url:
            return url
    except ImportError:
        pass
    fallback = {"bridge": BRIDGE_URL, "vision": VISION_URL}
    return f"{fallback.get(service, 'http://localhost')}{path}"


# ─── Tool Definitions ────────────────────────────────────────────────


def get_tool_definitions() -> list[dict]:
    """Return the list of MCP tool definitions."""
    return [
        # Memory tools
        {
            "name": "search_memory",
            "description": "Search memory for facts semantically related to a query.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"},
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results (default 10)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "store_memory",
            "description": "Store new content in memory. Extracts facts automatically.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The content to store"},
                    "content_type": {
                        "type": "string",
                        "description": "Type of content: conversation, email, decision, preference, technical",
                    },
                },
                "required": ["content", "content_type"],
            },
        },
        {
            "name": "get_stats",
            "description": "Get memory system statistics.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_entity",
            "description": "Look up an entity and its relationships in the knowledge graph.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The entity name to look up"}
                },
                "required": ["name"],
            },
        },
        # Vision tools
        {
            "name": "look",
            "description": "Look through the webcam — capture a snapshot and analyze it with vision AI.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "What to look for or analyze (default: general scene description)",
                    }
                },
            },
        },
        {
            "name": "who_is_here",
            "description": "Check who is currently visible or detected by the vision system.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "enroll_face",
            "description": "Enroll a person's face for future recognition. Person must be visible to the camera.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name of the person to enroll"}
                },
                "required": ["name"],
            },
        },
        {
            "name": "set_vision_mode",
            "description": "Switch the vision service mode. Modes: disarmed (no processing), basic (motion only), armed (full YOLO + face ID + escalation).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "description": "Vision mode: disarmed, basic, or armed",
                        "enum": ["disarmed", "basic", "armed"],
                    }
                },
                "required": ["mode"],
            },
        },
        # Memory block tools
        {
            "name": "memory_block_read",
            "description": "Read a named memory block. Blocks are persistent, structured working memory.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "block_name": {
                        "type": "string",
                        "description": "Name of the memory block to read",
                    }
                },
                "required": ["block_name"],
            },
        },
        {
            "name": "memory_block_write",
            "description": "Write/replace the content of a named memory block. Content is truncated to the block's max_chars limit.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "block_name": {
                        "type": "string",
                        "description": "Name of the memory block to write",
                    },
                    "content": {
                        "type": "string",
                        "description": "New content for the block (replaces existing)",
                    },
                },
                "required": ["block_name", "content"],
            },
        },
        {
            "name": "memory_block_list",
            "description": "List all memory blocks with their sizes, types, and last-updated timestamps.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        # CRM interaction
        {
            "name": "log_interaction",
            "description": "Log an interaction to the CRM. Creates conversation records and resolves contacts.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "contact_name": {"type": "string", "description": "Name of the contact"},
                    "channel": {
                        "type": "string",
                        "description": "Channel: email, telegram, voice, web, gchat, api",
                    },
                    "direction": {
                        "type": "string",
                        "description": "Direction: incoming or outgoing",
                        "enum": ["incoming", "outgoing"],
                    },
                    "content_summary": {
                        "type": "string",
                        "description": "Brief summary of the interaction",
                    },
                    "channel_identifier": {
                        "type": "string",
                        "description": "Channel-specific identifier (email address, phone number, etc.)",
                    },
                },
                "required": ["contact_name", "channel", "direction", "content_summary"],
            },
        },
        # CRM People
        {
            "name": "create_person",
            "description": "Create a person in the CRM. Returns the new person's ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "firstName": {"type": "string", "description": "First name"},
                    "lastName": {"type": "string", "description": "Last name"},
                    "email": {"type": "string", "description": "Email address"},
                    "phone": {"type": "string", "description": "Phone number"},
                },
                "required": ["firstName"],
            },
        },
        {
            "name": "get_person",
            "description": "Get a person's full profile by ID, including company info.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string", "description": "Person UUID"}},
                "required": ["id"],
            },
        },
        {
            "name": "update_person",
            "description": "Update a person's fields. Only provided fields are changed.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Person UUID"},
                    "firstName": {"type": "string"},
                    "lastName": {"type": "string"},
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                    "jobTitle": {"type": "string"},
                    "city": {"type": "string"},
                    "companyId": {"type": "string", "description": "Company UUID to link"},
                    "linkedinUrl": {"type": "string"},
                    "avatarUrl": {"type": "string"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "list_people",
            "description": "List people in the CRM, optionally filtered by search term.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Search by name"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
            },
        },
        {
            "name": "delete_person",
            "description": "Delete a person from the CRM (soft delete).",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string", "description": "Person UUID"}},
                "required": ["id"],
            },
        },
        # CRM Companies
        {
            "name": "create_company",
            "description": "Create a company in the CRM. Returns the new company's ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Company name"},
                    "domainName": {"type": "string", "description": "Website domain"},
                    "employees": {"type": "integer", "description": "Number of employees"},
                    "address": {"type": "string", "description": "Street address"},
                    "linkedinUrl": {"type": "string"},
                    "idealCustomerProfile": {"type": "boolean", "default": False},
                },
                "required": ["name"],
            },
        },
        {
            "name": "get_company",
            "description": "Get a company's profile by ID.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string", "description": "Company UUID"}},
                "required": ["id"],
            },
        },
        {
            "name": "update_company",
            "description": "Update a company's fields. Only provided fields are changed.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Company UUID"},
                    "name": {"type": "string"},
                    "domainName": {"type": "string"},
                    "employees": {"type": "integer"},
                    "address": {"type": "string"},
                    "linkedinUrl": {"type": "string"},
                    "idealCustomerProfile": {"type": "boolean"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "list_companies",
            "description": "List companies in the CRM, optionally filtered by name.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Search by company name"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 50)",
                        "default": 50,
                    },
                },
            },
        },
        {
            "name": "delete_company",
            "description": "Delete a company from the CRM (soft delete).",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string", "description": "Company UUID"}},
                "required": ["id"],
            },
        },
        # CRM Notes
        {
            "name": "create_note",
            "description": "Create a note in the CRM, optionally linked to a person or company.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Note title"},
                    "body": {"type": "string", "description": "Note body"},
                    "personId": {"type": "string", "description": "Person UUID to link"},
                    "companyId": {"type": "string", "description": "Company UUID to link"},
                },
                "required": ["title", "body"],
            },
        },
        {
            "name": "get_note",
            "description": "Get a note by ID.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string", "description": "Note UUID"}},
                "required": ["id"],
            },
        },
        {
            "name": "list_notes",
            "description": "List notes, optionally filtered by person or company.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "personId": {"type": "string", "description": "Filter by person UUID"},
                    "companyId": {"type": "string", "description": "Filter by company UUID"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 50)",
                        "default": 50,
                    },
                },
            },
        },
        {
            "name": "update_note",
            "description": "Update a note's fields.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Note UUID"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "personId": {"type": "string"},
                    "companyId": {"type": "string"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "delete_note",
            "description": "Delete a note from the CRM (soft delete).",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string", "description": "Note UUID"}},
                "required": ["id"],
            },
        },
        # CRM Tasks
        {
            "name": "create_task",
            "description": "Create a task in the CRM, optionally linked to a person or company. Use assignedToAgent for agent-to-agent coordination.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Task title"},
                    "body": {"type": "string", "description": "Task description"},
                    "status": {
                        "type": "string",
                        "description": "Status: TODO, IN_PROGRESS, REVIEW, DONE",
                        "default": "TODO",
                    },
                    "dueAt": {"type": "string", "description": "Due date (ISO 8601)"},
                    "personId": {"type": "string", "description": "Person UUID to link"},
                    "companyId": {"type": "string", "description": "Company UUID to link"},
                    "assignedToAgent": {
                        "type": "string",
                        "description": "Agent ID to assign (e.g. email-responder, supervisor)",
                    },
                    "createdByAgent": {
                        "type": "string",
                        "description": "Agent ID that created this task",
                    },
                    "priority": {
                        "type": "string",
                        "description": "Priority: low, normal, high, urgent",
                        "default": "normal",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags for categorization",
                    },
                    "parentTaskId": {
                        "type": "string",
                        "description": "Parent task UUID for subtask chains",
                    },
                },
                "required": ["title"],
            },
        },
        {
            "name": "get_task",
            "description": "Get a task by ID.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string", "description": "Task UUID"}},
                "required": ["id"],
            },
        },
        {
            "name": "list_tasks",
            "description": "List tasks, optionally filtered by status, person, agent, tags, or priority.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Filter by status"},
                    "personId": {"type": "string", "description": "Filter by person UUID"},
                    "assignedToAgent": {
                        "type": "string",
                        "description": "Filter by assigned agent",
                    },
                    "createdByAgent": {"type": "string", "description": "Filter by creating agent"},
                    "priority": {"type": "string", "description": "Filter by priority"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by tags (containment)",
                    },
                    "excludeResolved": {"type": "boolean", "description": "Exclude resolved tasks"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 50)",
                        "default": 50,
                    },
                },
            },
        },
        {
            "name": "update_task",
            "description": "Update a task's fields.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Task UUID"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "status": {"type": "string"},
                    "dueAt": {"type": "string"},
                    "personId": {"type": "string"},
                    "companyId": {"type": "string"},
                    "assignedToAgent": {"type": "string", "description": "Reassign to agent"},
                    "priority": {"type": "string", "description": "New priority"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New tags",
                    },
                    "resolution": {"type": "string", "description": "Resolution summary"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "delete_task",
            "description": "Delete a task from the CRM (soft delete).",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string", "description": "Task UUID"}},
                "required": ["id"],
            },
        },
        # CRM Task Coordination
        {
            "name": "resolve_task",
            "description": "Mark a task as DONE with a resolution summary.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Task UUID"},
                    "resolution": {
                        "type": "string",
                        "description": "What was done to complete the task",
                    },
                },
                "required": ["id", "resolution"],
            },
        },
        {
            "name": "list_agent_tasks",
            "description": "List tasks assigned to a specific agent, ordered by priority.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agentId": {
                        "type": "string",
                        "description": "Agent ID to get tasks for",
                    },
                    "includeUnassigned": {
                        "type": "boolean",
                        "description": "Include unassigned tasks",
                        "default": False,
                    },
                    "status": {"type": "string", "description": "Filter by status"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 50)",
                        "default": 50,
                    },
                },
                "required": ["agentId"],
            },
        },
        # CRM Task Review Workflow
        {
            "name": "approve_task",
            "description": "Approve a task in REVIEW status. Sends review_approved notification to assignee.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Task UUID"},
                    "resolution": {
                        "type": "string",
                        "description": "Approval resolution (e.g. 'Approved: reply looks good')",
                    },
                },
                "required": ["id", "resolution"],
            },
        },
        {
            "name": "reject_task",
            "description": "Reject a task in REVIEW status. Reverts to IN_PROGRESS, sends review_rejected notification.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Task UUID"},
                    "reason": {
                        "type": "string",
                        "description": "Why the task was rejected",
                    },
                    "changeRequests": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific changes requested (creates subtasks)",
                    },
                },
                "required": ["id", "reason"],
            },
        },
        # CRM Notifications
        {
            "name": "send_notification",
            "description": "Send a notification to another agent.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "fromAgent": {"type": "string", "description": "Sending agent ID"},
                    "toAgent": {"type": "string", "description": "Receiving agent ID"},
                    "notificationType": {
                        "type": "string",
                        "description": "Type: task_assigned, review_requested, review_approved, review_rejected, blocked, unblocked, agent_error, info, custom",
                    },
                    "subject": {"type": "string", "description": "Notification subject"},
                    "body": {"type": "string", "description": "Notification body"},
                    "metadata": {"type": "object", "description": "Additional metadata"},
                    "taskId": {"type": "string", "description": "Linked task UUID"},
                },
                "required": ["fromAgent", "toAgent", "notificationType", "subject"],
            },
        },
        {
            "name": "get_inbox",
            "description": "Get notification inbox for an agent.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agentId": {"type": "string", "description": "Agent ID"},
                    "unreadOnly": {
                        "type": "boolean",
                        "description": "Only unread notifications (default: true)",
                        "default": True,
                    },
                    "typeFilter": {
                        "type": "string",
                        "description": "Filter by notification type",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 50)",
                        "default": 50,
                    },
                },
                "required": ["agentId"],
            },
        },
        {
            "name": "ack_notification",
            "description": "Acknowledge a notification (mark as read + acknowledged).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "notificationId": {"type": "string", "description": "Notification UUID"},
                },
                "required": ["notificationId"],
            },
        },
        # Memory Block Append
        {
            "name": "append_to_block",
            "description": "Append a line to a memory block (atomic, auto-trims oldest entries). Used for shared working state.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "block_name": {
                        "type": "string",
                        "description": "Name of the memory block",
                    },
                    "entry": {
                        "type": "string",
                        "description": "Line to append (auto-prepends timestamp)",
                    },
                    "maxEntries": {
                        "type": "integer",
                        "description": "Max entries to keep (default 20)",
                        "default": 20,
                    },
                },
                "required": ["block_name", "entry"],
            },
        },
        # CRM Metadata
        {
            "name": "get_metadata_objects",
            "description": "List available CRM object types (tables) and their labels.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_object_metadata",
            "description": "Get column definitions for a CRM object type.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "objectName": {
                        "type": "string",
                        "description": "Table name: crm_people, crm_companies, crm_notes, crm_tasks, crm_conversations, crm_messages",
                    }
                },
                "required": ["objectName"],
            },
        },
        {
            "name": "search_records",
            "description": "Search across CRM tables by keyword.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keyword"},
                    "objectName": {"type": "string", "description": "Limit to specific table"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
        },
        # CRM Conversations
        {
            "name": "list_conversations",
            "description": "List CRM conversations filtered by status (open, resolved, pending, snoozed).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by status (default: open)",
                        "default": "open",
                    },
                    "page": {
                        "type": "integer",
                        "description": "Page number (default: 1)",
                        "default": 1,
                    },
                },
            },
        },
        {
            "name": "get_conversation",
            "description": "Get a single conversation by ID with contact info.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversationId": {"type": "integer", "description": "Conversation ID"}
                },
                "required": ["conversationId"],
            },
        },
        {
            "name": "list_messages",
            "description": "List all messages in a conversation.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversationId": {"type": "integer", "description": "Conversation ID"}
                },
                "required": ["conversationId"],
            },
        },
        {
            "name": "create_message",
            "description": "Create a message in a conversation.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversationId": {"type": "integer", "description": "Conversation ID"},
                    "content": {"type": "string", "description": "Message content"},
                    "messageType": {
                        "type": "string",
                        "description": "incoming or outgoing (default: outgoing)",
                        "default": "outgoing",
                    },
                    "private": {
                        "type": "boolean",
                        "description": "Private note (default: false)",
                        "default": False,
                    },
                },
                "required": ["conversationId", "content"],
            },
        },
        {
            "name": "toggle_conversation_status",
            "description": "Change a conversation's status (open, resolved, pending, snoozed).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversationId": {"type": "integer", "description": "Conversation ID"},
                    "status": {
                        "type": "string",
                        "description": "New status: open, resolved, pending, snoozed",
                    },
                },
                "required": ["conversationId", "status"],
            },
        },
        # ── Impetus One Healthcare Tools ──
        {
            "name": "search_patients",
            "description": "Search patients by name, date of birth, or medical condition.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query — patient name, DOB (YYYY-MM-DD), or condition",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10, max 50)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_patient_details",
            "description": "Get comprehensive patient record including demographics, allergies, and medications.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "patientId": {"type": "string", "description": "Patient ID (UUID)"},
                },
                "required": ["patientId"],
            },
        },
        {
            "name": "get_patient_clinical_notes",
            "description": "Get clinical notes and encounter history for a patient.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "patientId": {"type": "string", "description": "Patient ID (UUID)"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
                "required": ["patientId"],
            },
        },
        {
            "name": "get_patient_prescriptions",
            "description": "Get all prescriptions for a patient including medication, pharmacy, status, and dates.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "patientId": {"type": "string", "description": "Patient ID (UUID)"},
                    "status": {
                        "type": "string",
                        "description": "Filter by status (e.g., signed, transmitted, filled)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 50)",
                        "default": 50,
                    },
                },
                "required": ["patientId"],
            },
        },
        {
            "name": "search_prescriptions",
            "description": "Search prescriptions by medication name, patient name, status, or date range.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (medication name, patient name, etc.)",
                    },
                    "status": {"type": "string", "description": "Filter by status"},
                    "fromDate": {"type": "string", "description": "Filter from date (YYYY-MM-DD)"},
                    "toDate": {"type": "string", "description": "Filter to date (YYYY-MM-DD)"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_prescription_status",
            "description": "Get prescription status, transmission history, and details.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prescriptionId": {"type": "string", "description": "Prescription ID (UUID)"},
                },
                "required": ["prescriptionId"],
            },
        },
        {
            "name": "search_medications",
            "description": "Search medications by name, indication, or clinical use.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (medication name or indication)",
                    },
                    "indication": {
                        "type": "string",
                        "description": "Clinical indication to filter by",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "search_pharmacies",
            "description": "Search pharmacies by name or location.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (pharmacy name or location)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_appointments",
            "description": "Get upcoming or past appointments with optional date range.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "patientId": {
                        "type": "string",
                        "description": "Filter by patient ID (optional)",
                    },
                    "dateRange": {
                        "type": "string",
                        "description": "Date range: today, tomorrow, this_week, next_week, all (default: this_week)",
                        "default": "this_week",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
            },
        },
        {
            "name": "list_actable_providers",
            "description": "List all providers the authenticated user can act as, including scribe delegations with permissions.",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "create_prescription_draft",
            "description": "Create a new prescription draft for a patient. Requires provider review before signing.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "patientId": {"type": "string", "description": "Patient ID (UUID)"},
                    "medicationId": {"type": "string", "description": "Medication ID (UUID)"},
                    "directions": {
                        "type": "string",
                        "description": "Dosing directions for patient",
                    },
                    "quantity": {"type": "integer", "description": "Quantity to dispense"},
                    "daysSupply": {"type": "integer", "description": "Days supply"},
                    "refills": {
                        "type": "integer",
                        "description": "Number of refills (default: 0)",
                        "default": 0,
                    },
                    "notes": {"type": "string", "description": "Clinical notes (optional)"},
                    "actingAsProviderId": {
                        "type": "string",
                        "description": "Provider UUID to act as (for delegation)",
                    },
                },
                "required": ["patientId", "medicationId", "directions", "quantity", "daysSupply"],
            },
        },
        {
            "name": "schedule_appointment",
            "description": "Schedule a new appointment for a patient.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "patientId": {"type": "string", "description": "Patient ID (UUID)"},
                    "datetime": {
                        "type": "string",
                        "description": "Appointment date and time (YYYY-MM-DD HH:MM)",
                    },
                    "type": {
                        "type": "string",
                        "description": "Appointment type: video, in_person, phone",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason: consultation, follow_up, new_prescription, refill, lab_review, medication_review, other",
                    },
                    "duration": {
                        "type": "integer",
                        "description": "Duration in minutes (default: 30)",
                        "default": 30,
                    },
                },
                "required": ["patientId", "datetime", "type", "reason"],
            },
        },
        {
            "name": "transmit_prescription",
            "description": "Send a prescription to the pharmacy for filling. HIGH-RISK: requires human confirmation.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prescriptionId": {"type": "string", "description": "Prescription ID (UUID)"},
                    "confirmationId": {
                        "type": "string",
                        "description": "Confirmation ID from previous call (for 2-step confirmation)",
                    },
                    "actingAsProviderId": {
                        "type": "string",
                        "description": "Provider UUID to act as (for delegation)",
                    },
                },
                "required": ["prescriptionId"],
            },
        },
    ]


# ─── Tool Handlers ───────────────────────────────────────────────────


async def handle_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle an MCP tool call and return result."""

    # ── Memory tools ──

    if name == "search_memory":
        from robothor.memory.facts import search_facts

        results = await search_facts(arguments.get("query", ""), limit=arguments.get("limit", 10))
        return {
            "results": [
                {
                    "fact": r["fact_text"],
                    "category": r["category"],
                    "confidence": r["confidence"],
                    "similarity": round(r.get("similarity", 0), 4),
                }
                for r in results
            ]
        }

    elif name == "store_memory":
        from robothor.memory.facts import extract_facts, store_fact

        content = arguments.get("content", "")
        content_type = arguments.get("content_type", "conversation")
        facts = await extract_facts(content)
        if facts:
            stored_ids = [await store_fact(f, content, content_type) for f in facts]
            return {"id": stored_ids[0], "facts_stored": len(stored_ids)}
        fact = {"fact_text": content, "category": "personal", "entities": [], "confidence": 0.5}
        fact_id = await store_fact(fact, content, content_type)
        return {"id": fact_id, "facts_stored": 1}

    elif name == "get_stats":
        from robothor.memory.tiers import get_memory_stats

        return get_memory_stats()

    elif name == "get_entity":
        from robothor.memory.entities import get_entity

        try:
            result = await get_entity(arguments.get("name", ""))
            return result or {"name": arguments.get("name", ""), "found": False}
        except Exception:
            return {"name": arguments.get("name", ""), "found": False}

    # ── Vision proxy tools ──

    elif name == "look":
        prompt = arguments.get("prompt", "Describe what you see in this image in detail.")
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(_svc_url("vision", "/look"), json={"prompt": prompt})
                resp.raise_for_status()
                data = resp.json()
                return {
                    "description": data.get("description", ""),
                    "snapshot_path": data.get("snapshot_path", ""),
                }
        except Exception as e:
            return {"error": f"Vision look failed: {e}"}

    elif name == "who_is_here":
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(_svc_url("vision", "/health"))
                resp.raise_for_status()
                data = resp.json()
                return {
                    "people_present": data.get("people_present", []),
                    "running": data.get("running", False),
                    "mode": data.get("mode"),
                    "last_detection": data.get("last_detection"),
                }
        except Exception as e:
            return {"error": f"Vision status check failed: {e}"}

    elif name == "enroll_face":
        face_name = arguments.get("name", "")
        if not face_name:
            return {"error": "Name is required for face enrollment"}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(_svc_url("vision", "/enroll"), json={"name": face_name})
                resp.raise_for_status()
                return resp.json()  # type: ignore[no-any-return]
        except Exception as e:
            return {"error": f"Face enrollment failed: {e}"}

    elif name == "set_vision_mode":
        mode = arguments.get("mode", "")
        if mode not in ("disarmed", "basic", "armed"):
            return {"error": f"Invalid mode: {mode}. Valid: disarmed, basic, armed"}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(_svc_url("vision", "/mode"), json={"mode": mode})
                resp.raise_for_status()
                return resp.json()  # type: ignore[no-any-return]
        except Exception as e:
            return {"error": f"Mode switch failed: {e}"}

    # ── Memory block tools ──

    elif name == "memory_block_read":
        from robothor.memory.blocks import read_block

        return read_block(arguments.get("block_name", ""))  # type: ignore[no-any-return]

    elif name == "memory_block_write":
        from robothor.memory.blocks import write_block

        return write_block(arguments.get("block_name", ""), arguments.get("content", ""))  # type: ignore[no-any-return]

    elif name == "memory_block_list":
        from robothor.memory.blocks import list_blocks

        return list_blocks()  # type: ignore[no-any-return]

    # ── CRM interaction ──

    elif name == "log_interaction":
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    _svc_url("bridge", "/log-interaction"),
                    json={
                        k: arguments.get(k, "")
                        for k in [
                            "contact_name",
                            "channel",
                            "direction",
                            "content_summary",
                            "channel_identifier",
                        ]
                    },
                )
                resp.raise_for_status()
                return resp.json()  # type: ignore[no-any-return]
        except Exception as e:
            return {"error": f"Failed to log interaction: {e}"}

    # ── CRM People ──

    elif name == "create_person":
        from robothor.crm.dal import create_person

        person_id = create_person(
            arguments.get("firstName", ""),
            arguments.get("lastName", ""),
            arguments.get("email"),
            arguments.get("phone"),
        )
        return (
            {"id": person_id, "firstName": arguments.get("firstName", "")}
            if person_id
            else {"error": "Failed to create person"}
        )

    elif name == "get_person":
        from robothor.crm.dal import get_person

        return get_person(arguments["id"]) or {"error": "Person not found"}

    elif name == "update_person":
        from robothor.crm.dal import update_person

        pid = arguments.pop("id")
        field_map = {
            "firstName": "first_name",
            "lastName": "last_name",
            "email": "email",
            "phone": "phone",
            "jobTitle": "job_title",
            "city": "city",
            "companyId": "company_id",
            "linkedinUrl": "linkedin_url",
            "avatarUrl": "avatar_url",
        }
        kwargs = {
            dal_key: arguments[arg_key]
            for arg_key, dal_key in field_map.items()
            if arg_key in arguments
        }
        return {"success": update_person(pid, **kwargs), "id": pid}

    elif name == "list_people":
        from robothor.crm.dal import list_people

        results = list_people(search=arguments.get("search"), limit=arguments.get("limit", 20))
        return {"people": results, "count": len(results)}

    elif name == "delete_person":
        from robothor.crm.dal import delete_person

        return {"success": delete_person(arguments["id"]), "id": arguments["id"]}

    # ── CRM Companies ──

    elif name == "create_company":
        from robothor.crm.dal import create_company

        company_id = create_company(
            name=arguments.get("name", ""),
            domain_name=arguments.get("domainName"),
            employees=arguments.get("employees"),
            address=arguments.get("address"),
            linkedin_url=arguments.get("linkedinUrl"),
            ideal_customer_profile=arguments.get("idealCustomerProfile", False),
        )
        return (
            {"id": company_id, "name": arguments.get("name", "")}
            if company_id
            else {"error": "Failed to create company"}
        )

    elif name == "get_company":
        from robothor.crm.dal import get_company

        return get_company(arguments["id"]) or {"error": "Company not found"}

    elif name == "update_company":
        from robothor.crm.dal import update_company

        cid = arguments.pop("id")
        field_map = {
            "name": "name",
            "domainName": "domain_name",
            "employees": "employees",
            "address": "address",
            "linkedinUrl": "linkedin_url",
            "idealCustomerProfile": "ideal_customer_profile",
        }
        kwargs = {
            dal_key: arguments[arg_key]
            for arg_key, dal_key in field_map.items()
            if arg_key in arguments
        }
        return {"success": update_company(cid, **kwargs), "id": cid}

    elif name == "list_companies":
        from robothor.crm.dal import list_companies

        results = list_companies(search=arguments.get("search"), limit=arguments.get("limit", 50))
        return {"companies": results, "count": len(results)}

    elif name == "delete_company":
        from robothor.crm.dal import delete_company

        return {"success": delete_company(arguments["id"]), "id": arguments["id"]}

    # ── CRM Notes ──

    elif name == "create_note":
        from robothor.crm.dal import create_note

        note_id = create_note(
            title=arguments.get("title", ""),
            body=arguments.get("body", ""),
            person_id=arguments.get("personId"),
            company_id=arguments.get("companyId"),
        )
        return (
            {"id": note_id, "title": arguments.get("title", "")}
            if note_id
            else {"error": "Failed to create note"}
        )

    elif name == "get_note":
        from robothor.crm.dal import get_note

        return get_note(arguments["id"]) or {"error": "Note not found"}

    elif name == "list_notes":
        from robothor.crm.dal import list_notes

        results = list_notes(
            person_id=arguments.get("personId"),
            company_id=arguments.get("companyId"),
            limit=arguments.get("limit", 50),
        )
        return {"notes": results, "count": len(results)}

    elif name == "update_note":
        from robothor.crm.dal import update_note

        nid = arguments.pop("id")
        field_map = {
            "title": "title",
            "body": "body",
            "personId": "person_id",
            "companyId": "company_id",
        }
        kwargs = {
            dal_key: arguments[arg_key]
            for arg_key, dal_key in field_map.items()
            if arg_key in arguments
        }
        return {"success": update_note(nid, **kwargs), "id": nid}

    elif name == "delete_note":
        from robothor.crm.dal import delete_note

        return {"success": delete_note(arguments["id"]), "id": arguments["id"]}

    # ── CRM Tasks ──

    elif name == "create_task":
        from robothor.crm.dal import create_task

        task_id = create_task(
            title=arguments.get("title", ""),
            body=arguments.get("body"),
            status=arguments.get("status", "TODO"),
            due_at=arguments.get("dueAt"),
            person_id=arguments.get("personId"),
            company_id=arguments.get("companyId"),
            assigned_to_agent=arguments.get("assignedToAgent"),
            created_by_agent=arguments.get("createdByAgent"),
            priority=arguments.get("priority", "normal"),
            tags=arguments.get("tags"),
            parent_task_id=arguments.get("parentTaskId"),
        )
        return (
            {"id": task_id, "title": arguments.get("title", "")}
            if task_id
            else {"error": "Failed to create task"}
        )

    elif name == "get_task":
        from robothor.crm.dal import get_task

        return get_task(arguments["id"]) or {"error": "Task not found"}

    elif name == "list_tasks":
        from robothor.crm.dal import list_tasks

        results = list_tasks(
            status=arguments.get("status"),
            person_id=arguments.get("personId"),
            assigned_to_agent=arguments.get("assignedToAgent"),
            created_by_agent=arguments.get("createdByAgent"),
            priority=arguments.get("priority"),
            tags=arguments.get("tags"),
            exclude_resolved=arguments.get("excludeResolved", False),
            limit=arguments.get("limit", 50),
        )
        return {"tasks": results, "count": len(results)}

    elif name == "update_task":
        from robothor.crm.dal import update_task

        tid = arguments.pop("id")
        field_map = {
            "title": "title",
            "body": "body",
            "status": "status",
            "dueAt": "due_at",
            "personId": "person_id",
            "companyId": "company_id",
            "assignedToAgent": "assigned_to_agent",
            "priority": "priority",
            "tags": "tags",
            "resolution": "resolution",
        }
        kwargs = {
            dal_key: arguments[arg_key]
            for arg_key, dal_key in field_map.items()
            if arg_key in arguments
        }
        return {"success": update_task(tid, **kwargs), "id": tid}

    elif name == "delete_task":
        from robothor.crm.dal import delete_task

        return {"success": delete_task(arguments["id"]), "id": arguments["id"]}

    # ── Task Coordination ──

    elif name == "resolve_task":
        from robothor.crm.dal import resolve_task

        ok = resolve_task(
            task_id=arguments["id"],
            resolution=arguments.get("resolution", ""),
        )
        return {"success": ok, "id": arguments["id"]}

    elif name == "list_agent_tasks":
        from robothor.crm.dal import list_agent_tasks

        results = list_agent_tasks(
            agent_id=arguments.get("agentId", ""),
            include_unassigned=arguments.get("includeUnassigned", False),
            status=arguments.get("status"),
            limit=arguments.get("limit", 50),
        )
        return {"tasks": results, "count": len(results)}

    # ── Task Review Workflow ──

    elif name == "approve_task":
        from robothor.crm.dal import approve_task

        approve_result: bool | dict = approve_task(
            task_id=arguments["id"],
            resolution=arguments.get("resolution", "Approved"),
            reviewer="mcp-user",
        )
        if isinstance(approve_result, dict) and "error" in approve_result:
            return approve_result
        return {"success": True, "id": arguments["id"]}

    elif name == "reject_task":
        from robothor.crm.dal import reject_task

        reject_result: bool | dict = reject_task(
            task_id=arguments["id"],
            reason=arguments.get("reason", ""),
            reviewer="mcp-user",
            change_requests=arguments.get("changeRequests"),
        )
        if isinstance(reject_result, dict) and "error" in reject_result:
            return reject_result
        return {"success": True, "id": arguments["id"]}

    # ── Notifications ──

    elif name == "send_notification":
        from robothor.crm.dal import send_notification

        nid = send_notification(
            from_agent=arguments.get("fromAgent", ""),
            to_agent=arguments.get("toAgent", ""),
            notification_type=arguments.get("notificationType", ""),
            subject=arguments.get("subject", ""),
            body=arguments.get("body"),
            metadata=arguments.get("metadata"),
            task_id=arguments.get("taskId"),
        )
        return (
            {"id": nid, "subject": arguments.get("subject", "")}
            if nid
            else {"error": "Failed to send notification"}
        )

    elif name == "get_inbox":
        from robothor.crm.dal import get_agent_inbox

        results = get_agent_inbox(
            agent_id=arguments.get("agentId", ""),
            unread_only=arguments.get("unreadOnly", True),
            type_filter=arguments.get("typeFilter"),
            limit=arguments.get("limit", 50),
        )
        return {"notifications": results, "count": len(results)}

    elif name == "ack_notification":
        from robothor.crm.dal import acknowledge_notification

        ok = acknowledge_notification(arguments.get("notificationId", ""))
        return {"success": ok, "id": arguments.get("notificationId", "")}

    # ── Memory Block Append ──

    elif name == "append_to_block":
        from robothor.crm.dal import append_to_block

        ok = append_to_block(
            block_name=arguments.get("block_name", ""),
            entry=arguments.get("entry", ""),
            max_entries=arguments.get("maxEntries", 20),
        )
        return {"success": ok, "block_name": arguments.get("block_name", "")}

    # ── CRM Metadata ──

    elif name == "get_metadata_objects":
        from robothor.crm.dal import get_metadata_objects

        return {"objects": get_metadata_objects()}

    elif name == "get_object_metadata":
        from robothor.crm.dal import get_object_metadata

        return get_object_metadata(arguments.get("objectName", "")) or {"error": "Object not found"}

    elif name == "search_records":
        from robothor.crm.dal import search_records

        results = search_records(
            query=arguments.get("query", ""),
            object_name=arguments.get("objectName"),
            limit=arguments.get("limit", 20),
        )
        return {"results": results, "count": len(results)}

    # ── CRM Conversations ──

    elif name == "list_conversations":
        from robothor.crm.dal import list_conversations

        convos = list_conversations(
            status=arguments.get("status", "open"), page=arguments.get("page", 1)
        )
        return {"conversations": convos, "count": len(convos)}

    elif name == "get_conversation":
        from robothor.crm.dal import get_conversation

        return get_conversation(arguments["conversationId"]) or {"error": "Conversation not found"}

    elif name == "list_messages":
        from robothor.crm.dal import list_messages

        return {"payload": list_messages(arguments["conversationId"])}

    elif name == "create_message":
        from robothor.crm.dal import send_message

        result = send_message(
            conversation_id=arguments["conversationId"],
            content=arguments.get("content", ""),
            message_type=arguments.get("messageType", "outgoing"),
            private=arguments.get("private", False),
        )
        return dict(result) if result else {"error": "Failed to create message"}

    elif name == "toggle_conversation_status":
        from robothor.crm.dal import toggle_conversation_status

        ok = toggle_conversation_status(
            conversation_id=arguments["conversationId"], status=arguments.get("status", "resolved")
        )
        return {"success": ok, "conversationId": arguments["conversationId"]}

    return {"error": f"Unknown tool: {name}"}


# ─── MCP Server ──────────────────────────────────────────────────────


def create_server():
    """Create and configure the MCP server."""
    import mcp.types as types
    from mcp.server import Server

    server = Server("robothor-memory")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=d["name"], description=d["description"], inputSchema=d["inputSchema"])
            for d in get_tool_definitions()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        result = await handle_tool_call(name, arguments or {})
        return [types.TextContent(type="text", text=json.dumps(result, default=str))]

    return server


async def run_server():
    """Run the MCP server with stdio transport."""
    from mcp.server.stdio import stdio_server

    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run_server())
