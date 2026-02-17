"""Twenty CRM API client using GraphQL with session-based access tokens."""
import httpx
import config

_access_token: str | None = None


async def _get_token(client: httpx.AsyncClient) -> str:
    """Get a fresh access token via login flow."""
    global _access_token
    # Step 1: login token
    r = await client.post(f"{config.TWENTY_URL}/graphql", json={
        "query": f'mutation {{ getLoginTokenFromCredentials(email: "{config.TWENTY_EMAIL}", password: "{config.TWENTY_PASSWORD}") {{ loginToken {{ token }} }} }}'
    })
    lt = r.json()["data"]["getLoginTokenFromCredentials"]["loginToken"]["token"]
    # Step 2: access token
    r = await client.post(f"{config.TWENTY_URL}/graphql", json={
        "query": f'mutation {{ getAuthTokensFromLoginToken(loginToken: "{lt}") {{ tokens {{ accessToken {{ token }} }} }} }}'
    })
    _access_token = r.json()["data"]["getAuthTokensFromLoginToken"]["tokens"]["accessToken"]["token"]
    return _access_token


async def gql(query: str, client: httpx.AsyncClient) -> dict:
    """Execute a GraphQL query against Twenty CRM."""
    global _access_token
    if not _access_token:
        await _get_token(client)

    r = await client.post(f"{config.TWENTY_URL}/graphql", json={"query": query},
                          headers={"Authorization": f"Bearer {_access_token}"})
    data = r.json()

    # Retry once on auth error
    if data.get("errors") and "UNAUTHENTICATED" in str(data["errors"]):
        await _get_token(client)
        r = await client.post(f"{config.TWENTY_URL}/graphql", json={"query": query},
                              headers={"Authorization": f"Bearer {_access_token}"})
        data = r.json()
    return data


async def search_people(name: str, client: httpx.AsyncClient) -> list:
    """Search for people by name."""
    escaped = name.replace('"', '\\"')
    r = await gql(f'''{{ people(filter: {{ or: [
        {{ name: {{ firstName: {{ ilike: "%{escaped}%" }} }} }},
        {{ name: {{ lastName: {{ ilike: "%{escaped}%" }} }} }}
    ] }}) {{ edges {{ node {{ id name {{ firstName lastName }} emails {{ primaryEmail }} }} }} }} }}''', client)
    if r.get("data"):
        return [e["node"] for e in r["data"]["people"]["edges"]]
    return []


async def create_person(first_name: str, last_name: str, email: str | None,
                        phone: str | None, client: httpx.AsyncClient) -> str | None:
    """Create a person in Twenty CRM. Returns person ID."""
    parts = [f'name: {{ firstName: "{first_name}", lastName: "{last_name}" }}']
    if email:
        parts.append(f'emails: {{ primaryEmail: "{email}" }}')
    if phone:
        parts.append(f'phones: {{ primaryPhoneNumber: "{phone}" }}')

    r = await gql(f'mutation {{ createPerson(data: {{ {", ".join(parts)} }}) {{ id }} }}', client)
    if r.get("data") and r["data"].get("createPerson"):
        return r["data"]["createPerson"]["id"]
    return None


async def get_person(person_id: str, client: httpx.AsyncClient) -> dict | None:
    """Get a person by ID."""
    r = await gql(f'{{ person(id: "{person_id}") {{ id name {{ firstName lastName }} emails {{ primaryEmail }} phones {{ primaryPhoneNumber }} jobTitle company {{ id name }} }} }}', client)
    if r.get("data") and r["data"].get("person"):
        return r["data"]["person"]
    return None


async def create_note(title: str, body: str, client: httpx.AsyncClient) -> str | None:
    """Create a note in Twenty CRM. Returns note ID."""
    escaped_title = title.replace('"', '\\"').replace('\n', '\\n')
    escaped_body = body.replace('"', '\\"').replace('\n', '\\n')
    r = await gql(f'mutation {{ createNote(data: {{ title: "{escaped_title}", body: "{escaped_body}" }}) {{ id }} }}', client)
    if r.get("data") and r["data"].get("createNote"):
        return r["data"]["createNote"]["id"]
    return None


async def update_person(person_id: str, client: httpx.AsyncClient,
                        job_title: str | None = None,
                        company_id: str | None = None,
                        city: str | None = None) -> bool:
    """Update a person's fields in Twenty CRM. Only sets non-None fields.

    Returns True if update succeeded.
    """
    parts = []
    if job_title is not None:
        escaped = job_title.replace('"', '\\"')
        parts.append(f'jobTitle: "{escaped}"')
    if company_id is not None:
        parts.append(f'companyId: "{company_id}"')
    if city is not None:
        escaped = city.replace('"', '\\"')
        parts.append(f'city: "{escaped}"')

    if not parts:
        return False

    r = await gql(
        f'mutation {{ updatePerson(id: "{person_id}", data: {{ {", ".join(parts)} }}) {{ id }} }}',
        client,
    )
    return bool(r.get("data") and r["data"].get("updatePerson"))


async def find_or_create_company(name: str, client: httpx.AsyncClient) -> str | None:
    """Find a company by name, or create it. Returns company ID."""
    escaped = name.replace('"', '\\"')
    # Search first
    r = await gql(
        f'{{ companies(filter: {{ name: {{ ilike: "%{escaped}%" }} }}) '
        f'{{ edges {{ node {{ id name }} }} }} }}',
        client,
    )
    if r.get("data"):
        edges = r["data"]["companies"]["edges"]
        if edges:
            return edges[0]["node"]["id"]

    # Create
    r = await gql(
        f'mutation {{ createCompany(data: {{ name: "{escaped}" }}) {{ id }} }}',
        client,
    )
    if r.get("data") and r["data"].get("createCompany"):
        return r["data"]["createCompany"]["id"]
    return None


async def list_people(search: str | None = None, limit: int = 20,
                      client: httpx.AsyncClient = None) -> list:
    """List people, optionally filtered by search term."""
    if search:
        return await search_people(search, client)

    r = await gql(f'{{ people(first: {limit}, orderBy: {{ updatedAt: DescNullsLast }}) {{ edges {{ node {{ id name {{ firstName lastName }} emails {{ primaryEmail }} phones {{ primaryPhoneNumber }} jobTitle company {{ id name }} }} }} }} }}', client)
    if r.get("data"):
        return [e["node"] for e in r["data"]["people"]["edges"]]
    return []
