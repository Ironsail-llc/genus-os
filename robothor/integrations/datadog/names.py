"""The environment names of the Datadog integration, and nothing else.

No imports, on purpose: :mod:`robothor.secrets.classification` reads these and
must stay import-light, so it cannot import the client (which pulls in httpx
and the settings model).
"""

API_KEY_ENV = "ROBOTHOR_DATADOG_API_KEY"
APP_KEY_ENV = "ROBOTHOR_DATADOG_APP_KEY"
#: The tenant -> filter map. Not a credential, but it IS the wall between
#: tenants, so no agent tool may write it.
SCOPES_ENV = "ROBOTHOR_DATADOG_SCOPES"
