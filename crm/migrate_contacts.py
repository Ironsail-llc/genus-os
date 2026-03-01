#!/usr/bin/env python3
"""
Migrate contacts from brain/memory/contacts.json → Twenty CRM.
Creates Person records, Company records, and links them.
Outputs contact_id_map.json for later reference.

Usage: python3 migrate_contacts.py
"""

import json
import os
import time
import urllib.error
import urllib.request

TWENTY_URL = "http://localhost:3030"
CONTACTS_FILE = "/home/philip/clawd/memory/contacts.json"
OUTPUT_FILE = "/home/philip/robothor/crm/contact_id_map.json"
TWENTY_EMAIL = os.getenv("TWENTY_EMAIL", "robothor@ironsail.ai")
TWENTY_PASSWORD = os.getenv("TWENTY_PASSWORD", "")

# Skip non-person entries
SKIP_NAMES = {"Twilio Notifications", "ngrok Team", "Arc Notifications", "Robothor"}


def gql(query, token):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    data = json.dumps({"query": query}).encode()
    req = urllib.request.Request(f"{TWENTY_URL}/graphql", data=data, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            resp = json.loads(r.read())
            if resp.get("errors"):
                print(f"  GQL error: {resp['errors'][0]['message']}")
            return resp
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode()[:200]}")
        return {"errors": [{"message": f"HTTP {e.code}"}]}


def get_access_token():
    # No auth needed for login
    headers = {"Content-Type": "application/json"}
    login_query = f'mutation {{ getLoginTokenFromCredentials(email: "{TWENTY_EMAIL}", password: "{TWENTY_PASSWORD}") {{ loginToken {{ token }} }} }}'
    data = json.dumps({"query": login_query}).encode()
    req = urllib.request.Request(f"{TWENTY_URL}/graphql", data=data, headers=headers)
    with urllib.request.urlopen(req) as r:
        lt = json.loads(r.read())["data"]["getLoginTokenFromCredentials"]["loginToken"]["token"]

    data = json.dumps(
        {
            "query": f'mutation {{ getAuthTokensFromLoginToken(loginToken: "{lt}") {{ tokens {{ accessToken {{ token }} }} }} }}'
        }
    ).encode()
    req = urllib.request.Request(f"{TWENTY_URL}/graphql", data=data, headers=headers)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())["data"]["getAuthTokensFromLoginToken"]["tokens"]["accessToken"][
            "token"
        ]


def find_or_create_company(name, token):
    """Find existing company by name or create new one."""
    ename = name.replace('"', '\\"')
    r = gql(
        f'{{ companies(filter: {{ name: {{ eq: "{ename}" }} }}) {{ edges {{ node {{ id name }} }} }} }}',
        token,
    )
    if r.get("data") and r["data"]["companies"]["edges"]:
        cid = r["data"]["companies"]["edges"][0]["node"]["id"]
        print(f"    Found company: {name} ({cid})")
        return cid

    r = gql(f'mutation {{ createCompany(data: {{ name: "{ename}" }}) {{ id name }} }}', token)
    if r.get("data") and r["data"].get("createCompany"):
        cid = r["data"]["createCompany"]["id"]
        print(f"    Created company: {name} ({cid})")
        return cid
    return None


def split_name(name):
    """Split a name into first/last, handling single names."""
    parts = name.strip().split(None, 1)
    first = parts[0] if parts else name
    last = parts[1] if len(parts) > 1 else ""
    return first.replace('"', '\\"'), last.replace('"', '\\"')


def create_person(contact, company_id, token):
    """Create a person in Twenty CRM."""
    first, last = split_name(contact["name"])
    email = contact.get("email", "")
    phone = contact.get("phone", "")
    role = contact.get("role", "")

    email_part = f'emails: {{ primaryEmail: "{email}" }}' if email else ""
    phone_part = f'phones: {{ primaryPhoneNumber: "{phone}" }}' if phone else ""
    company_part = f'companyId: "{company_id}"' if company_id else ""
    job_part = f'jobTitle: "{role.replace(chr(34), chr(92) + chr(34))}"' if role else ""

    parts = [f'name: {{ firstName: "{first}", lastName: "{last}" }}']
    if email_part:
        parts.append(email_part)
    if phone_part:
        parts.append(phone_part)
    if job_part:
        parts.append(job_part)
    if company_part:
        parts.append(company_part)

    mutation = f"mutation {{ createPerson(data: {{ {', '.join(parts)} }}) {{ id name {{ firstName lastName }} }} }}"
    r = gql(mutation, token)
    if r.get("data") and r["data"].get("createPerson"):
        pid = r["data"]["createPerson"]["id"]
        print(f"  Created: {contact['name']} ({pid})")
        return pid
    return None


def create_note(person_id, content, token):
    """Create a note attached to a person."""
    escaped = content.replace('"', '\\"').replace("\n", "\\n")
    # Twenty notes are created via noteTargets
    r = gql(
        f'''mutation {{ createNote(data: {{
        title: "Migration Notes"
        body: "{escaped}"
        noteTargets: {{ createMany: [{{ personId: "{person_id}" }}] }}
    }}) {{ id }} }}''',
        token,
    )
    return r.get("data") is not None


def main():
    print("=== Contact Migration: contacts.json → Twenty CRM ===\n")

    # Load contacts
    with open(CONTACTS_FILE) as f:
        contacts = json.load(f)["contacts"]
    print(f"Loaded {len(contacts)} contacts\n")

    # Get access token
    token = get_access_token()
    print("Authenticated with Twenty CRM\n")

    # First, delete sample/seed data
    r = gql("{ people { edges { node { id name { firstName lastName } } } } }", token)
    if r.get("data"):
        for edge in r["data"]["people"]["edges"]:
            pid = edge["node"]["id"]
            name = f"{edge['node']['name']['firstName']} {edge['node']['name']['lastName']}"
            gql(f'mutation {{ deletePerson(id: "{pid}") {{ id }} }}', token)
            print(f"  Deleted seed person: {name}")

    r = gql("{ companies { edges { node { id name } } } }", token)
    if r.get("data"):
        for edge in r["data"]["companies"]["edges"]:
            cid = edge["node"]["id"]
            gql(f'mutation {{ deleteCompany(id: "{cid}") {{ id }} }}', token)
            print(f"  Deleted seed company: {edge['node']['name']}")

    print()

    # Migrate contacts
    id_map = {}
    companies_cache = {}
    seen_emails = set()

    for contact in contacts:
        name = contact.get("name", "Unknown")

        # Skip non-person entries and duplicates
        if name in SKIP_NAMES:
            print(f"  Skipped: {name}")
            continue
        email = contact.get("email", "")
        if email and email in seen_emails:
            print(f"  Skipped duplicate: {name} ({email})")
            continue
        if email:
            seen_emails.add(email)

        print(f"\nMigrating: {name}")

        # Handle company
        company_id = None
        company_name = contact.get("company", "")
        if company_name:
            if company_name in companies_cache:
                company_id = companies_cache[company_name]
            else:
                company_id = find_or_create_company(company_name, token)
                if company_id:
                    companies_cache[company_name] = company_id

        # Create person
        person_id = create_person(contact, company_id, token)
        if not person_id:
            print(f"  FAILED to create: {name}")
            continue

        # Create note from recent activity
        activity = contact.get("recentActivity", [])
        if activity:
            note_lines = [f"[{a.get('date', '?')}] {a.get('note', '')}" for a in activity[:10]]
            note_content = "Migrated activity notes:\\n" + "\\n".join(note_lines)
            create_note(person_id, note_content, token)

        # Build ID map entry
        id_map[name] = {
            "twenty_person_id": person_id,
            "twenty_company_id": company_id,
            "email": email,
            "phone": contact.get("phone", ""),
            "company": company_name,
        }

        time.sleep(0.1)  # Rate limiting

    # Save ID map
    with open(OUTPUT_FILE, "w") as f:
        json.dump(id_map, f, indent=2)
    print(f"\n=== Migration complete: {len(id_map)} contacts migrated ===")
    print(f"ID map saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
