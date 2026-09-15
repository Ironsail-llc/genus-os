/**
 * One account, reduced to the fields the Helm is allowed to know about.
 *
 * `crm/bridge/routers/users.py::_account_payload` already assembles its answer
 * field by field rather than spreading the row, precisely so that a column
 * added to `user_accounts` cannot widen what an operator's browser receives.
 * This is the same lock on the other side of the wire, and it is not
 * redundant: the dashboard and the bridge are versioned separately and
 * deployed separately, so a bridge that grows `password_hash`,
 * `mfa_secret_enc` or `idp_subject` in its listing — by a `SELECT *` that
 * creeps back into the DAL, by a plugin, or by a version skew — must not be
 * able to paint one onto a screen simply because the page spread the object it
 * was handed.
 *
 * So the row component never renders the payload. It renders THIS, which is
 * built from a literal list of keys and drops everything else on the floor.
 */

export interface Account {
  id: string;
  email: string;
  display_name: string;
  role: string;
  status: string;
  sso_bound: boolean;
  mfa_enabled: boolean;
  last_login_at: string | null;
  created_at: string | null;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function maybeText(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

/** One account, or `null` for a row with no id — which is not addressable. */
export function normalizeAccount(raw: unknown): Account | null {
  if (!raw || typeof raw !== "object") return null;
  const row = raw as Record<string, unknown>;
  const id = text(row.id);
  if (!id) return null;
  return {
    id,
    email: text(row.email),
    display_name: text(row.display_name),
    role: text(row.role),
    status: text(row.status),
    sso_bound: Boolean(row.sso_bound),
    mfa_enabled: Boolean(row.mfa_enabled),
    last_login_at: maybeText(row.last_login_at),
    created_at: maybeText(row.created_at),
  };
}

/** The `users` array of `GET /api/users`, defended against a shape it is not. */
export function normalizeAccounts(body: unknown): Account[] {
  const users = (body as { users?: unknown })?.users;
  if (!Array.isArray(users)) return [];
  return users
    .map(normalizeAccount)
    .filter((account): account is Account => account !== null);
}

/** A grant as the invite and binding-grant routes answer with it. */
export interface BindingGrant {
  id: string;
  expires_at: string | null;
  /** The one IdP that may spend it, or `null` for any configured issuer. */
  issuer: string | null;
}

export function normalizeGrant(raw: unknown): BindingGrant | null {
  if (!raw || typeof raw !== "object") return null;
  const row = raw as Record<string, unknown>;
  const id = text(row.id);
  if (!id) return null;
  return { id, expires_at: maybeText(row.expires_at), issuer: maybeText(row.issuer) };
}
