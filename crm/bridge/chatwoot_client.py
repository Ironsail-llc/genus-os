"""Chatwoot API client."""
import httpx
import config

HEADERS = {"api_access_token": config.CHATWOOT_API_TOKEN, "Content-Type": "application/json"}
BASE = f"{config.CHATWOOT_URL}/api/v1/accounts/{config.CHATWOOT_ACCOUNT_ID}"


async def search_contacts(query: str, client: httpx.AsyncClient) -> list:
    """Search contacts by name or email."""
    r = await client.get(f"{BASE}/contacts/search", params={"q": query}, headers=HEADERS)
    if r.status_code == 200:
        return r.json().get("payload", [])
    return []


async def create_contact(name: str, email: str | None = None,
                         phone: str | None = None, identifier: str | None = None,
                         client: httpx.AsyncClient = None) -> dict | None:
    """Create a contact in Chatwoot. Returns contact dict."""
    data = {"name": name, "inbox_id": config.CHATWOOT_INBOX_ID}
    if email:
        data["email"] = email
    if phone:
        data["phone_number"] = phone
    if identifier:
        data["identifier"] = identifier
    r = await client.post(f"{BASE}/contacts", json=data, headers=HEADERS)
    if r.status_code in (200, 201):
        return r.json().get("payload", {}).get("contact", r.json())
    return None


async def get_contact(contact_id: int, client: httpx.AsyncClient) -> dict | None:
    """Get contact by ID."""
    r = await client.get(f"{BASE}/contacts/{contact_id}", headers=HEADERS)
    if r.status_code == 200:
        return r.json()
    return None


async def create_conversation(contact_id: int, inbox_id: int | None = None,
                              client: httpx.AsyncClient = None) -> dict | None:
    """Create a conversation for a contact."""
    r = await client.post(f"{BASE}/conversations", json={
        "contact_id": contact_id,
        "inbox_id": inbox_id or config.CHATWOOT_INBOX_ID,
    }, headers=HEADERS)
    if r.status_code in (200, 201):
        return r.json()
    return None


async def send_message(conversation_id: int, content: str, message_type: str = "incoming",
                       client: httpx.AsyncClient = None) -> dict | None:
    """Send a message to a conversation. message_type: incoming or outgoing."""
    r = await client.post(f"{BASE}/conversations/{conversation_id}/messages", json={
        "content": content,
        "message_type": message_type,
    }, headers=HEADERS)
    if r.status_code in (200, 201):
        return r.json()
    return None


async def get_conversations(contact_id: int, client: httpx.AsyncClient) -> list:
    """Get all conversations for a contact."""
    r = await client.get(f"{BASE}/contacts/{contact_id}/conversations", headers=HEADERS)
    if r.status_code == 200:
        return r.json().get("payload", [])
    return []


async def list_conversations(status: str = "open", page: int = 1,
                             client: httpx.AsyncClient = None) -> dict:
    """List conversations by status. Returns {data: {payload: [...], meta: {...}}}."""
    r = await client.get(f"{BASE}/conversations", params={"status": status, "page": page},
                         headers=HEADERS)
    if r.status_code == 200:
        return r.json()
    return {"data": {"payload": [], "meta": {}}}


async def get_conversation(conversation_id: int, client: httpx.AsyncClient) -> dict | None:
    """Get a single conversation by ID."""
    r = await client.get(f"{BASE}/conversations/{conversation_id}", headers=HEADERS)
    if r.status_code == 200:
        return r.json()
    return None


async def list_messages(conversation_id: int, client: httpx.AsyncClient) -> list:
    """List all messages in a conversation."""
    r = await client.get(f"{BASE}/conversations/{conversation_id}/messages", headers=HEADERS)
    if r.status_code == 200:
        return r.json().get("payload", [])
    return []


async def toggle_conversation_status(conversation_id: int, status: str,
                                      client: httpx.AsyncClient) -> dict | None:
    """Toggle conversation status: open, resolved, pending, snoozed."""
    r = await client.post(f"{BASE}/conversations/{conversation_id}/toggle_status",
                          json={"status": status}, headers=HEADERS)
    if r.status_code in (200, 201):
        return r.json()
    return None
