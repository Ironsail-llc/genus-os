#!/usr/bin/env python3
"""vault_client.py — Vaultwarden API client for Robothor agents.

Provides read/write access to the Vaultwarden password vault.
All encryption/decryption handled transparently.

Supports cipher types:
    1 = Login (username, password, URIs)
    3 = Card (cardholder, number, expMonth, expYear, CVV, brand)

Usage as library:
    from vault_client import VaultClient
    vc = VaultClient()
    vc.login()
    items = vc.list_items()
    item = vc.get_item("AD Mortgage")
    vc.create_login("Site Name", "user", "pass", uri="https://example.com")
    vc.create_card("My Visa", cardholderName="Philip D", number="4111...",
                   expMonth="03", expYear="2028", code="123", brand="Visa")

Usage as CLI:
    python vault_client.py list
    python vault_client.py get "AD Mortgage"
    python vault_client.py search "staging"
    python vault_client.py create --name "Site" --username "user" --password "pass"
    python vault_client.py create-card --name "My Visa" --cardholder "Philip D" --number "4111..." --exp-month "03" --exp-year "2028" --code "123" --brand "Visa"
"""

import argparse
import base64
import hashlib
import hmac as hmac_mod
import json
import os
import secrets
import sys


def _get_vault_url():
    try:
        from memory_system.service_registry import get_service_url

        url = get_service_url("vaultwarden")
        if url:
            return url
    except ImportError:
        pass
    return "http://localhost:8222"


VAULT_URL = _get_vault_url()
VAULT_EMAIL = "robothor@ironsail.ai"


def _get_master_password():
    """Get the vault master password from environment (SOPS-injected)."""
    pw = os.environ.get("VAULTWARDEN_MASTER_PASSWORD")
    if not pw:
        secrets_path = "/run/robothor/secrets.env"
        if os.path.exists(secrets_path):
            with open(secrets_path) as f:
                for line in f:
                    if line.startswith("VAULTWARDEN_MASTER_PASSWORD="):
                        pw = line.strip().split("=", 1)[1]
                        # Strip shell quoting
                        if pw and pw[0] in ("'", '"') and pw[-1] == pw[0]:
                            pw = pw[1:-1]
                        break
    if not pw:
        raise RuntimeError("VAULTWARDEN_MASTER_PASSWORD not found in environment or secrets.env")
    return pw


def _derive_key(master_password, email):
    """Bitwarden-compatible PBKDF2 key derivation."""
    return hashlib.pbkdf2_hmac(
        "sha256", master_password.encode("utf-8"), email.lower().encode("utf-8"), 600000, dklen=32
    )


def _make_password_hash(key, master_password):
    """Bitwarden-compatible master password hash."""
    h = hashlib.pbkdf2_hmac("sha256", key, master_password.encode("utf-8"), 1, dklen=32)
    return base64.b64encode(h).decode("utf-8")


def _decrypt_aes(enc_key, mac_key, enc_string):
    """Decrypt a Bitwarden '2.iv|ct|mac' encrypted string."""
    if not enc_string or not enc_string.startswith("2."):
        return enc_string

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    parts = enc_string[2:].split("|")
    iv = base64.b64decode(parts[0])
    ct = base64.b64decode(parts[1])
    mac = base64.b64decode(parts[2])

    expected_mac = hmac_mod.new(mac_key, iv + ct, hashlib.sha256).digest()
    if not hmac_mod.compare_digest(mac, expected_mac):
        return "[MAC verification failed]"

    cipher = Cipher(algorithms.AES(enc_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ct) + decryptor.finalize()
    pad_len = padded[-1]
    return padded[:-pad_len].decode("utf-8")


def _encrypt_aes(enc_key, mac_key, plaintext):
    """Encrypt a string to Bitwarden '2.iv|ct|mac' format."""
    if not plaintext:
        return None

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    data = plaintext.encode("utf-8")
    iv = secrets.token_bytes(16)
    cipher = Cipher(algorithms.AES(enc_key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    pad_len = 16 - (len(data) % 16)
    padded = data + bytes([pad_len] * pad_len)
    ct = encryptor.update(padded) + encryptor.finalize()
    mac = hmac_mod.new(mac_key, iv + ct, hashlib.sha256).digest()
    return f"2.{base64.b64encode(iv).decode()}|{base64.b64encode(ct).decode()}|{base64.b64encode(mac).decode()}"


class VaultClient:
    """Vaultwarden API client with transparent encryption."""

    def __init__(self, url=VAULT_URL, email=VAULT_EMAIL):
        self.url = url
        self.email = email
        self.access_token = None
        self.enc_key = None
        self.mac_key = None

    def login(self):
        """Authenticate and retrieve encryption keys."""
        import httpx
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand

        master_password = _get_master_password()
        key = _derive_key(master_password, self.email)
        password_hash = _make_password_hash(key, master_password)

        # OAuth2 login
        resp = httpx.post(
            f"{self.url}/identity/connect/token",
            data={
                "grant_type": "password",
                "username": self.email,
                "password": password_hash,
                "scope": "api offline_access",
                "client_id": "cli",
                "deviceType": "9",
                "deviceIdentifier": "robothor-cli-agent",
                "deviceName": "Robothor CLI",
                "devicePushToken": "",
            },
            timeout=30,
        )

        if resp.status_code != 200:
            raise RuntimeError(f"Login failed ({resp.status_code}): {resp.text[:200]}")

        self.access_token = resp.json()["access_token"]

        # Get encrypted symmetric key from profile
        sync = httpx.get(f"{self.url}/api/sync", headers=self._headers(), timeout=30).json()
        enc_key_str = sync["profile"]["key"]

        # Decrypt symmetric key
        parts = enc_key_str[2:].split("|")
        iv = base64.b64decode(parts[0])
        ct = base64.b64decode(parts[1])
        mac = base64.b64decode(parts[2])

        stretched_enc = HKDFExpand(algorithm=hashes.SHA256(), length=32, info=b"enc").derive(key)
        stretched_mac = HKDFExpand(algorithm=hashes.SHA256(), length=32, info=b"mac").derive(key)

        expected_mac = hmac_mod.new(stretched_mac, iv + ct, hashlib.sha256).digest()
        if not hmac_mod.compare_digest(mac, expected_mac):
            raise RuntimeError("Symmetric key MAC verification failed")

        cipher = Cipher(algorithms.AES(stretched_enc), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded = decryptor.update(ct) + decryptor.finalize()
        pad_len = padded[-1]
        sym_key = padded[:-pad_len]

        self.enc_key = sym_key[:32]
        self.mac_key = sym_key[32:64]

    def _headers(self):
        return {"Authorization": f"Bearer {self.access_token}"}

    def _decrypt(self, enc_string):
        return _decrypt_aes(self.enc_key, self.mac_key, enc_string)

    def _encrypt(self, plaintext):
        return _encrypt_aes(self.enc_key, self.mac_key, plaintext)

    def list_items(self):
        """List all vault items (decrypted names, no secrets)."""
        import httpx

        resp = httpx.get(f"{self.url}/api/ciphers", headers=self._headers(), timeout=30)
        items = resp.json().get("data", [])
        result = []
        for item in items:
            entry = {
                "id": item["id"],
                "type": item["type"],
                "name": self._decrypt(item.get("name")),
            }
            login = item.get("login")
            if login:
                entry["username"] = self._decrypt(login.get("username"))
                uris = login.get("uris") or []
                entry["uri"] = self._decrypt(uris[0]["uri"]) if uris else None
            card = item.get("card")
            if card:
                entry["brand"] = self._decrypt(card.get("brand"))
                number = self._decrypt(card.get("number"))
                if number and len(number) >= 4:
                    entry["last4"] = number[-4:]
                else:
                    entry["last4"] = None
            result.append(entry)
        return result

    def get_item(self, name_or_id):
        """Get a specific vault item by name or ID, fully decrypted."""
        import httpx

        # Try by ID first
        resp = httpx.get(
            f"{self.url}/api/ciphers/{name_or_id}", headers=self._headers(), timeout=30
        )
        if resp.status_code == 200:
            return self._decrypt_item(resp.json())

        # Search by name
        items = self.list_items()
        for item in items:
            if item["name"] and name_or_id.lower() in item["name"].lower():
                resp = httpx.get(
                    f"{self.url}/api/ciphers/{item['id']}", headers=self._headers(), timeout=30
                )
                if resp.status_code == 200:
                    return self._decrypt_item(resp.json())

        return None

    def search(self, query):
        """Search vault items by name (case-insensitive)."""
        items = self.list_items()
        query_lower = query.lower()
        return [i for i in items if i.get("name") and query_lower in i["name"].lower()]

    def create_login(self, name, username, password, uri=None, notes=None):
        """Create a new login item in the vault."""
        import httpx

        item = {
            "type": 1,
            "name": self._encrypt(name),
            "notes": self._encrypt(notes) if notes else None,
            "login": {
                "username": self._encrypt(username),
                "password": self._encrypt(password),
                "uris": [{"uri": self._encrypt(uri), "match": None}] if uri else None,
            },
            "folderId": None,
            "organizationId": None,
            "collectionIds": None,
            "reprompt": 0,
        }
        resp = httpx.post(f"{self.url}/api/ciphers", json=item, headers=self._headers(), timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"Create failed ({resp.status_code}): {resp.text[:200]}")
        return self._decrypt_item(resp.json())

    def create_card(
        self, name, cardholderName, number, expMonth, expYear, code=None, brand=None, notes=None
    ):
        """Create a new card item in the vault.

        Args:
            name: Display name for the card (e.g. "Chase Sapphire")
            cardholderName: Name on card
            number: Full card number
            expMonth: Expiration month ("01"-"12")
            expYear: Expiration year ("2028")
            code: CVV/security code (optional)
            brand: Card brand — Visa, Mastercard, Amex, Discover, etc. (optional)
            notes: Additional notes (optional)
        """
        import httpx

        item = {
            "type": 3,
            "name": self._encrypt(name),
            "notes": self._encrypt(notes) if notes else None,
            "card": {
                "cardholderName": self._encrypt(cardholderName),
                "number": self._encrypt(number),
                "expMonth": self._encrypt(str(expMonth)),
                "expYear": self._encrypt(str(expYear)),
                "code": self._encrypt(code) if code else None,
                "brand": self._encrypt(brand) if brand else None,
            },
            "folderId": None,
            "organizationId": None,
            "collectionIds": None,
            "reprompt": 0,
        }
        resp = httpx.post(f"{self.url}/api/ciphers", json=item, headers=self._headers(), timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"Create card failed ({resp.status_code}): {resp.text[:200]}")
        return self._decrypt_item(resp.json())

    def delete_item(self, item_id):
        """Delete a vault item by ID."""
        import httpx

        resp = httpx.delete(
            f"{self.url}/api/ciphers/{item_id}", headers=self._headers(), timeout=30
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Delete failed ({resp.status_code}): {resp.text[:200]}")
        return True

    def _decrypt_item(self, raw):
        """Fully decrypt a raw cipher item from the API."""
        result = {
            "id": raw["id"],
            "type": raw["type"],
            "name": self._decrypt(raw.get("name")),
            "notes": self._decrypt(raw.get("notes")),
        }
        login = raw.get("login")
        if login:
            result["username"] = self._decrypt(login.get("username"))
            result["password"] = self._decrypt(login.get("password"))
            uris = login.get("uris") or []
            result["uris"] = [self._decrypt(u["uri"]) for u in uris]
        card = raw.get("card")
        if card:
            result["cardholderName"] = self._decrypt(card.get("cardholderName"))
            result["number"] = self._decrypt(card.get("number"))
            result["expMonth"] = self._decrypt(card.get("expMonth"))
            result["expYear"] = self._decrypt(card.get("expYear"))
            result["code"] = self._decrypt(card.get("code"))
            result["brand"] = self._decrypt(card.get("brand"))
        return result


def main():
    parser = argparse.ArgumentParser(description="Robothor Vault Client")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List all vault items")

    get_p = sub.add_parser("get", help="Get a vault item by name")
    get_p.add_argument("name", help="Item name (partial match)")

    search_p = sub.add_parser("search", help="Search vault items")
    search_p.add_argument("query", help="Search query")

    create_p = sub.add_parser("create", help="Create a login item")
    create_p.add_argument("--name", required=True)
    create_p.add_argument("--username", required=True)
    create_p.add_argument("--password", required=True)
    create_p.add_argument("--uri")
    create_p.add_argument("--notes")

    card_p = sub.add_parser("create-card", help="Create a card item")
    card_p.add_argument("--name", required=True)
    card_p.add_argument("--cardholder", required=True)
    card_p.add_argument("--number", required=True)
    card_p.add_argument("--exp-month", required=True)
    card_p.add_argument("--exp-year", required=True)
    card_p.add_argument("--code", help="CVV")
    card_p.add_argument("--brand", help="Visa, Mastercard, Amex, Discover, etc.")
    card_p.add_argument("--notes")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    vc = VaultClient()
    vc.login()

    if args.command == "list":
        items = vc.list_items()
        for item in items:
            user = item.get("username", "")
            uri = item.get("uri", "")
            print(f"  {item['name']:<30} {user:<30} {uri}")

    elif args.command == "get":
        item = vc.get_item(args.name)
        if item:
            print(json.dumps(item, indent=2))
        else:
            print(f"No item found matching '{args.name}'", file=sys.stderr)
            sys.exit(1)

    elif args.command == "search":
        items = vc.search(args.query)
        for item in items:
            print(f"  {item['name']:<30} {item.get('username', '')}")

    elif args.command == "create":
        item = vc.create_login(
            args.name,
            args.username,
            args.password,
            uri=args.uri,
            notes=args.notes,
        )
        print(f"Created: {item['name']} (id: {item['id']})")

    elif args.command == "create-card":
        item = vc.create_card(
            args.name,
            cardholderName=args.cardholder,
            number=args.number,
            expMonth=args.exp_month,
            expYear=args.exp_year,
            code=args.code,
            brand=args.brand,
            notes=args.notes,
        )
        print(f"Created card: {item['name']} (id: {item['id']})")


if __name__ == "__main__":
    main()
