import json
import re
import sys

# Add the directory containing crm_dal to the Python path
sys.path.insert(0, "/home/philip/robothor/crm/bridge")

import crm_dal


def find_thread_id(body):
    """Extracts threadId from the task body."""
    if not body:
        return None
    # Look for a line like 'threadId: <some_id>'
    match = re.search(r"threadId:\s*(\w+)", body, re.IGNORECASE)
    if match:
        return match.group(1)
    # Look for a URL containing a threadId
    match = re.search(r"threads/(\w+)", body)
    if match:
        return match.group(1)
    return None


def main():
    """Fetches and prints non-resolved tasks for specified agents."""
    agents = ["email-responder", "email-analyst"]
    all_tasks_info = []

    for agent in agents:
        tasks = crm_dal.list_tasks(assigned_to_agent=agent, exclude_resolved=True)
        for task in tasks:
            thread_id = find_thread_id(task.get("body"))
            task_info = {
                "id": task.get("id"),
                "title": task.get("title"),
                "body": task.get("body"),
                "threadId": thread_id,
            }
            all_tasks_info.append(task_info)

    print(json.dumps(all_tasks_info, indent=2))


if __name__ == "__main__":
    main()
