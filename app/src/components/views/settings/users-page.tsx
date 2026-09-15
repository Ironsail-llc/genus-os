"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Copy, KeyRound, Loader2, RefreshCw, UserPlus } from "lucide-react";

import { PageHeader } from "@/components/business/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { readBridgeReply } from "@/lib/bridge/read-reply";
import { useRowActions } from "@/lib/bridge/row-actions";
import {
  normalizeAccounts,
  normalizeGrant,
  type Account,
  type BindingGrant,
} from "@/lib/users/accounts";

/**
 * Users & roles: who can sign in to this instance, and as what.
 *
 * The shapes are `crm/bridge/routers/users.py`. Three properties this page is
 * built around, each of which the bridge also enforces — a UI that disagreed
 * with the server would simply produce refusals nobody can act on:
 *
 * **Nothing secret-shaped is rendered.** The row is drawn from
 * `normalizeAccount`, a fixed list of keys, not from the payload. The bridge
 * projects `password_hash`, `mfa_secret_enc` and `idp_subject` away twice
 * already; this is the third lock, on the side of the wire that is versioned
 * separately from the one making that promise.
 *
 * **A refusal is rendered in the server's words.** "only an owner may grant
 * the owner role", "this is the tenant's only owner — …", "you cannot demote
 * or disable your own account — …": every one of those names the thing to do
 * instead, and there is nothing this page could say that would be better. It
 * does not retry any of them, because none of them is transient.
 *
 * **Ownership cannot be handed over here.** Migration 071 caps a tenant at one
 * owner ROW, so "promote somebody else first" is not a move the API can make:
 * the second promotion is a 409 against the database's own index. The owner
 * role is therefore offered only to an owner (who will still be refused a
 * second one, and told why), and the page says where the change actually
 * happens.
 *
 * The page polls the account listing every 60 s **while visible**, and holds
 * no secret of its own: a binding grant has no token — the grant IS the row,
 * consumed by the next verified SSO claim for that address — so what is shown
 * once is its expiry, its issuer, and what the invitee has to do.
 */

const BRIDGE = "/api/bridge";
const POLL_MS = 60_000;

const OWNER_ROLE = "owner";

export interface RoleEntry {
  id: string;
  description: string;
}

interface InviteForm {
  email: string;
  role: string;
  display_name: string;
  sso: boolean;
}

const EMPTY_INVITE: InviteForm = { email: "", role: "member", display_name: "", sso: false };

function when(value: string | null): string {
  if (!value) return "never";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function expiry(value: string | null): string {
  if (!value) return "an expiry the bridge did not report";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function statusClass(status: string): string {
  if (status === "active") return "border-success/30 bg-success/10 text-success";
  if (status === "invited") return "border-info/30 bg-info/10 text-info";
  if (status === "disabled") return "border-destructive/30 bg-destructive/10 text-destructive";
  return "border-border bg-muted text-muted-foreground";
}

/**
 * What the invitee must do, in one paragraph an operator can paste anywhere.
 *
 * There is no secret in a binding grant, so "send them this code" would be
 * wrong twice: there is no code, and the thing that makes the grant safe is
 * that only a VERIFIED claim for that exact address can spend it.
 */
export function signInInstructions(email: string, grant: BindingGrant): string {
  const issuer = grant.issuer
    ? `Sign in with ${grant.issuer}`
    : "Sign in with this instance's identity provider";
  return (
    `${issuer} using the address ${email}, before ${expiry(grant.expires_at)}. ` +
    `There is no code to enter and nothing to copy: the first verified sign-in for that ` +
    `address binds the account. After it expires, ask an operator to arm a new one.`
  );
}

export interface UsersPageProps {
  /** The Settings container unmounts an inactive page; this is the poll gate. */
  visible?: boolean;
  /** Session role. UX only — the bridge authorizes every route here itself. */
  role?: string | null;
}

export function UsersPage({ visible = true, role }: UsersPageProps) {
  const [users, setUsers] = useState<Account[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [roles, setRoles] = useState<RoleEntry[]>([]);
  const [rolesError, setRolesError] = useState<string | null>(null);

  const [inviteOpen, setInviteOpen] = useState(false);
  const [invite, setInvite] = useState<InviteForm>(EMPTY_INVITE);
  const [inviting, setInviting] = useState(false);
  const [inviteError, setInviteError] = useState<string | null>(null);
  const [created, setCreated] = useState<{ account: Account; grant: BindingGrant | null } | null>(
    null
  );
  const [copied, setCopied] = useState(false);

  const [confirming, setConfirming] = useState<string | null>(null);
  const [grants, setGrants] = useState<Record<string, BindingGrant>>({});
  const { busyRow, rowErrors, rowNotes, setRowNote, act } = useRowActions();

  const callerIsOwner = role === OWNER_ROLE;

  const loadRef = useRef<() => Promise<void>>(async () => {});

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${BRIDGE}/api/users`);
      if (!res.ok) {
        setListError(await readBridgeReply(res));
        return;
      }
      setUsers(normalizeAccounts(await res.json()));
      setListError(null);
    } catch {
      setListError("The dashboard could not reach the bridge. Check that the service is running.");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadRoles = useCallback(async () => {
    try {
      const res = await fetch(`${BRIDGE}/api/auth/roles`);
      if (!res.ok) {
        setRolesError(await readBridgeReply(res));
        return;
      }
      const body = (await res.json()) as { roles?: RoleEntry[] };
      setRoles(Array.isArray(body.roles) ? body.roles : []);
      setRolesError(null);
    } catch {
      setRolesError("The dashboard could not reach the bridge to read the role catalog.");
    }
  }, []);

  loadRef.current = async () => {
    await load();
  };

  useEffect(() => {
    if (!visible) return;
    void loadRoles();
    void loadRef.current();
    const timer = setInterval(() => void loadRef.current(), POLL_MS);
    return () => clearInterval(timer);
  }, [visible, loadRoles]);

  /**
   * The roles this caller may actually assign.
   *
   * `owner` is hidden from an admin rather than merely refused: the bridge
   * answers 403 "only an owner may grant the owner role", and offering a
   * choice whose only outcome is that sentence is a worse screen than not
   * offering it.
   */
  const assignableRoles = useMemo(
    () => (callerIsOwner ? roles : roles.filter((entry) => entry.id !== OWNER_ROLE)),
    [roles, callerIsOwner]
  );

  const describeRole = useCallback(
    (id: string) => roles.find((entry) => entry.id === id)?.description ?? "",
    [roles]
  );

  /** Merge one updated account back into the listing, in place. */
  const absorb = useCallback((updated: Account) => {
    setUsers((prev) =>
      prev ? prev.map((user) => (user.id === updated.id ? updated : user)) : prev
    );
  }, []);

  async function patchUser(user: Account, patch: Record<string, unknown>, note: string) {
    await act(
      user.id,
      `${BRIDGE}/api/users/${encodeURIComponent(user.id)}`,
      { method: "PATCH", body: JSON.stringify(patch) },
      (body) => {
        const updated = normalizeAccounts({ users: [(body as { user?: unknown })?.user] })[0];
        if (updated) absorb(updated);
        setConfirming(null);
        setRowNote(user.id, note);
      }
    );
  }

  async function armGrant(user: Account) {
    await act(
      user.id,
      `${BRIDGE}/api/users/${encodeURIComponent(user.id)}/binding-grant`,
      { method: "POST" },
      (body) => {
        const grant = normalizeGrant((body as { grant?: unknown })?.grant);
        if (grant) setGrants((prev) => ({ ...prev, [user.id]: grant }));
      }
    );
  }

  async function submitInvite() {
    const email = invite.email.trim();
    if (!email) return;
    setInviting(true);
    setInviteError(null);
    try {
      const payload: Record<string, unknown> = { email, role: invite.role, sso: invite.sso };
      const displayName = invite.display_name.trim();
      if (displayName) payload.display_name = displayName;

      const res = await fetch(`${BRIDGE}/api/users`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        setInviteError(await readBridgeReply(res));
        return;
      }
      const body = (await res.json()) as { user?: unknown; grant?: unknown };
      const account = normalizeAccounts({ users: [body.user] })[0];
      if (!account) {
        setInviteError("The bridge created the account but answered in a shape this page cannot read.");
        return;
      }
      setCreated({ account, grant: normalizeGrant(body.grant) });
      setCopied(false);
      setInvite(EMPTY_INVITE);
      setInviteOpen(false);
      await load();
    } catch {
      setInviteError("The dashboard could not reach the bridge to create that account.");
    } finally {
      setInviting(false);
    }
  }

  async function copyInstructions(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      // A browser that refuses the clipboard is not a failure worth a banner:
      // the instructions are on the screen and can be selected by hand, which
      // is why they are rendered as text rather than hidden behind the button.
      setCopied(false);
    }
  }

  const rows = users ?? [];

  return (
    <div className="flex flex-col gap-4 p-4" data-testid="settings-page-users">
      <PageHeader title="Users & roles" description="Who can sign in, and as what.">
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            void loadRoles();
            void loadRef.current();
          }}
          data-testid="users-refresh"
        >
          <RefreshCw aria-hidden />
          Refresh
        </Button>
      </PageHeader>

      <p className="max-w-3xl text-xs text-muted-foreground">
        Accounts created here can sign in to the Helm; they are not CRM people and have no channel
        identity. Ownership cannot be transferred from this screen — an instance holds exactly one
        owner row, so changing it is <span className="font-mono">genus user</span> on the box.
      </p>

      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          data-testid="users-invite-open"
          onClick={() => {
            setInviteError(null);
            setConfirming(null);
            setInviteOpen((prev) => !prev);
          }}
        >
          <UserPlus aria-hidden />
          Invite somebody
        </Button>
        {rolesError ? (
          <span className="text-xs text-warning" data-testid="users-roles-error">
            The role catalog could not be read ({rolesError}), so the role menus below are empty.
          </span>
        ) : null}
      </div>

      {inviteOpen ? (
        <form
          data-testid="users-invite-form"
          className="flex max-w-2xl flex-col gap-2 rounded-lg border border-border bg-card p-3"
          onSubmit={(event) => {
            event.preventDefault();
            void submitInvite();
          }}
        >
          <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-end">
            <label className="flex flex-1 flex-col gap-1 text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
              Email
              <Input
                data-testid="invite-email"
                type="email"
                autoComplete="off"
                placeholder="carol@example.com"
                value={invite.email}
                onChange={(event) => setInvite((prev) => ({ ...prev, email: event.target.value }))}
              />
            </label>
            <label className="flex flex-1 flex-col gap-1 text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
              Display name
              <Input
                data-testid="invite-display-name"
                autoComplete="off"
                placeholder="defaults to the address"
                value={invite.display_name}
                onChange={(event) =>
                  setInvite((prev) => ({ ...prev, display_name: event.target.value }))
                }
              />
            </label>
            <label className="flex flex-col gap-1 text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
              Role
              <NativeSelect
                data-testid="invite-role"
                value={invite.role}
                onChange={(event) => setInvite((prev) => ({ ...prev, role: event.target.value }))}
              >
                {assignableRoles.map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    {entry.id}
                  </option>
                ))}
              </NativeSelect>
            </label>
          </div>

          <p className="text-[11px] text-muted-foreground">{describeRole(invite.role)}</p>

          <label className="flex items-center gap-2 text-xs text-foreground">
            <input
              data-testid="invite-sso"
              type="checkbox"
              checked={invite.sso}
              onChange={(event) => setInvite((prev) => ({ ...prev, sso: event.target.checked }))}
            />
            They sign in with the organisation&apos;s identity provider
          </label>
          <p className="text-[11px] text-muted-foreground">
            With SSO the account is created active and a one-shot binding grant is armed, so the
            invitee&apos;s first verified sign-in binds it. Without SSO the account is created as an
            invitation with no way in until somebody runs{" "}
            <span className="font-mono">genus user set-password</span> on the box. No mail is sent
            either way — pass the instructions on yourself.
          </p>

          <div className="flex flex-wrap items-center gap-1.5">
            <Button
              type="submit"
              size="xs"
              disabled={inviting || !invite.email.trim()}
              data-testid="invite-submit"
            >
              {inviting ? <Loader2 aria-hidden className="animate-spin" /> : null}
              Create account
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="xs"
              data-testid="invite-cancel"
              onClick={() => {
                setInvite(EMPTY_INVITE);
                setInviteOpen(false);
                setInviteError(null);
              }}
            >
              Cancel
            </Button>
          </div>

          {inviteError ? (
            <p className="text-xs text-destructive" data-testid="invite-error">
              {inviteError}
            </p>
          ) : null}
        </form>
      ) : null}

      {created && created.grant ? (
        <div
          className="flex max-w-2xl flex-col gap-2 rounded-lg border border-info/30 bg-info/5 p-3"
          data-testid="invite-grant"
        >
          <p className="text-sm font-medium text-foreground">
            {created.account.email} can sign in once, and only once, with this grant
          </p>
          <p className="text-xs text-muted-foreground">
            {signInInstructions(created.account.email, created.grant)}
          </p>
          <p className="text-[11px] text-muted-foreground">
            This panel is shown once. Nothing here is a secret, but nothing here comes back either —
            arm a new grant from the row below if it is lost.
          </p>
          <div className="flex flex-wrap items-center gap-1.5">
            <Button
              variant="outline"
              size="xs"
              data-testid="invite-grant-copy"
              onClick={() =>
                void copyInstructions(signInInstructions(created.account.email, created.grant!))
              }
            >
              <Copy aria-hidden />
              {copied ? "Copied" : "Copy the sign-in instructions"}
            </Button>
            <Button
              variant="ghost"
              size="xs"
              data-testid="invite-grant-dismiss"
              onClick={() => setCreated(null)}
            >
              Done
            </Button>
          </div>
        </div>
      ) : null}

      {created && !created.grant ? (
        <div
          className="flex max-w-2xl flex-col gap-2 rounded-lg border border-border bg-card p-3"
          data-testid="invite-created"
        >
          <p className="text-sm font-medium text-foreground">
            {created.account.email} exists as an invitation
          </p>
          <p className="text-xs text-muted-foreground">
            Nothing can sign in as it yet. Finish it on the box with{" "}
            <span className="font-mono">genus user set-password</span>, or arm an SSO grant from the
            row below.
          </p>
          <div>
            <Button
              variant="ghost"
              size="xs"
              data-testid="invite-created-dismiss"
              onClick={() => setCreated(null)}
            >
              Done
            </Button>
          </div>
        </div>
      ) : null}

      <div className="flex flex-col gap-2" data-testid="users-page">
        {loading && !users ? (
          <div
            className="flex items-center gap-2 rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground"
            data-testid="users-loading"
          >
            <Loader2 aria-hidden className="size-4 animate-spin" />
            Reading the accounts from the bridge…
          </div>
        ) : null}

        {listError ? (
          <div
            className="flex flex-col gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-4"
            data-testid="users-error"
          >
            <p className="text-sm font-medium text-destructive">The account listing failed</p>
            <p className="text-xs text-muted-foreground">{listError}</p>
            <div>
              <Button variant="outline" size="sm" onClick={() => void loadRef.current()}>
                Try again
              </Button>
            </div>
          </div>
        ) : null}

        {!listError && !loading && rows.length === 0 ? (
          <div
            className="rounded-lg border border-dashed border-border bg-card/40 p-4"
            data-testid="users-empty"
          >
            <p className="text-sm font-medium text-foreground">This instance has no accounts</p>
            <p className="mt-1 max-w-xl text-xs text-muted-foreground">
              Not even an owner, which means nobody can sign in to the Helm at all. Run{" "}
              <span className="font-mono">genus user add</span> on the box, or invite somebody
              above.
            </p>
          </div>
        ) : null}

        {rows.map((user) => {
          const grant = grants[user.id];
          const busy = busyRow === user.id;
          return (
            <div
              key={user.id}
              data-testid={`user-row-${user.id}`}
              className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium text-foreground">
                  {user.display_name || user.email}
                </span>
                <span className="font-mono text-xs text-muted-foreground">{user.email}</span>
                <Badge
                  variant="outline"
                  className={statusClass(user.status)}
                  data-testid={`user-status-${user.id}`}
                >
                  {user.status || "status unknown"}
                </Badge>
              </div>

              <dl className="grid grid-cols-1 gap-x-6 gap-y-1 text-xs sm:grid-cols-3">
                <div className="flex flex-wrap items-baseline gap-1.5">
                  <dt className="text-muted-foreground">SSO</dt>
                  <dd className="text-foreground" data-testid={`user-sso-${user.id}`}>
                    {user.sso_bound ? "bound" : "not bound"}
                  </dd>
                </div>
                <div className="flex flex-wrap items-baseline gap-1.5">
                  <dt className="text-muted-foreground">MFA</dt>
                  <dd className="text-foreground" data-testid={`user-mfa-${user.id}`}>
                    {user.mfa_enabled ? "enabled" : "off"}
                  </dd>
                </div>
                <div className="flex flex-wrap items-baseline gap-1.5">
                  <dt className="text-muted-foreground">Last signed in</dt>
                  <dd className="text-foreground" data-testid={`user-last-login-${user.id}`}>
                    {when(user.last_login_at)}
                  </dd>
                </div>
              </dl>

              <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
                <label className="flex items-center gap-2 text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
                  Role
                  <NativeSelect
                    data-testid={`user-role-${user.id}`}
                    disabled={busy}
                    value={user.role}
                    onChange={(event) =>
                      void patchUser(
                        user,
                        { role: event.target.value },
                        `Role changed to ${event.target.value}.`
                      )
                    }
                  >
                    {assignableRoles.some((entry) => entry.id === user.role) ? null : (
                      // The role the account actually holds, even where this
                      // caller may not assign it: a select that silently
                      // showed something else would misreport the account.
                      <option value={user.role}>{user.role || "unknown"}</option>
                    )}
                    {assignableRoles.map((entry) => (
                      <option key={entry.id} value={entry.id}>
                        {entry.id}
                      </option>
                    ))}
                  </NativeSelect>
                </label>

                <Button
                  variant="outline"
                  size="xs"
                  disabled={busy}
                  data-testid={`user-grant-arm-${user.id}`}
                  onClick={() => void armGrant(user)}
                >
                  <KeyRound aria-hidden />
                  Arm an SSO grant
                </Button>

                {user.status === "disabled" ? null : (
                  <Button
                    variant="outline"
                    size="xs"
                    disabled={busy}
                    data-testid={`user-deactivate-${user.id}`}
                    onClick={() => setConfirming(user.id)}
                  >
                    Deactivate
                  </Button>
                )}
              </div>

              <p className="text-[11px] text-muted-foreground" data-testid={`user-role-help-${user.id}`}>
                {describeRole(user.role) || "The bridge did not describe this role."}
              </p>

              {confirming === user.id ? (
                <div
                  role="alertdialog"
                  aria-live="assertive"
                  aria-label={`Deactivate ${user.email}?`}
                  className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-background px-2 py-1.5"
                >
                  <span className="text-xs text-foreground">
                    Deactivate {user.email}? Their live sessions are revoked immediately.
                  </span>
                  <Button
                    variant="destructive"
                    size="xs"
                    disabled={busy}
                    data-testid={`user-deactivate-confirm-${user.id}`}
                    onClick={() =>
                      void patchUser(user, { status: "disabled" }, "Deactivated; sessions revoked.")
                    }
                  >
                    Deactivate
                  </Button>
                  <Button
                    variant="ghost"
                    size="xs"
                    data-testid={`user-deactivate-cancel-${user.id}`}
                    onClick={() => setConfirming(null)}
                  >
                    Keep it active
                  </Button>
                </div>
              ) : null}

              {grant ? (
                <div
                  className="flex flex-col gap-1 rounded-md border border-info/30 bg-info/5 px-2 py-1.5"
                  data-testid={`user-grant-${user.id}`}
                >
                  <span className="text-xs text-foreground">
                    {signInInstructions(user.email, grant)}
                  </span>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <Button
                      variant="ghost"
                      size="xs"
                      data-testid={`user-grant-copy-${user.id}`}
                      onClick={() => void copyInstructions(signInInstructions(user.email, grant))}
                    >
                      <Copy aria-hidden />
                      Copy the sign-in instructions
                    </Button>
                    <Button
                      variant="ghost"
                      size="xs"
                      data-testid={`user-grant-dismiss-${user.id}`}
                      onClick={() =>
                        setGrants((prev) => {
                          const next = { ...prev };
                          delete next[user.id];
                          return next;
                        })
                      }
                    >
                      Done
                    </Button>
                  </div>
                </div>
              ) : null}

              {rowErrors[user.id] ? (
                <p className="text-xs text-destructive" data-testid={`user-error-${user.id}`}>
                  {rowErrors[user.id]}
                </p>
              ) : null}
              {rowNotes[user.id] ? (
                <p className="text-xs text-success" data-testid={`user-note-${user.id}`}>
                  {rowNotes[user.id]}
                </p>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}
