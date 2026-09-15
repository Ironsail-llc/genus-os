"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2, Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { readBridgeReply } from "@/lib/bridge/read-reply";
import { useRowActions } from "@/lib/bridge/row-actions";

/**
 * Who is waiting to be let in on one channel, and who is already bound.
 *
 * Mounted only once the operator opens a channel's access panel, and that is
 * deliberate: the counts on the cards come from `GET /api/channels`, which
 * composes them in ONE round trip for every channel, while this panel costs
 * two requests per channel. Loading it eagerly would put ten requests behind
 * a page whose first question is usually "is Telegram set up".
 *
 * The pairing code is the thing this panel cannot show. `GET /pending`
 * deliberately answers with neither the code nor the sender's native id — the
 * router's own header calls a compromised operator session reading those the
 * request it would most want — so the operator types the six characters the
 * sender was given. The rows are therefore evidence that somebody is knocking
 * and when their code dies, and the form beside each one is where the code
 * they read out over the phone goes.
 *
 * `PAIRABLE_ROLES` mirrors `robothor/engine/channels/identities.py`: a flow
 * whose first step is "a stranger sent a message" never ends in an admin.
 */

const BRIDGE = "/api/bridge";

/** Mirrors robothor/engine/channels/identities.py::PAIRABLE_ROLES. */
const PAIRABLE_ROLES = ["viewer", "member"];

/** Mirrors identities.DEFAULT_PAIRED_ROLE. */
const DEFAULT_PAIRED_ROLE = "viewer";

/** Mirrors identities.PAIRING_CODE_LENGTH. */
const CODE_LENGTH = 6;

export interface PendingPairing {
  id: string;
  channel: string;
  expires_at: string | null;
  created_at: string | null;
  display_name_present: boolean;
}

export interface ChannelIdentity {
  id: string;
  user_id: string | null;
  native_id_fingerprint: string | null;
  display_name: string | null;
  role: string | null;
  paired_at: string | null;
  paired_by: string | null;
}

interface SettleForm {
  code: string;
  email: string;
  role: string;
}

function when(value: string | null): string {
  if (!value) return "unknown";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function ChannelAccess({
  channel,
  onSettled,
}: {
  channel: string;
  /** The card above owns the pending COUNT; a decision here changes it. */
  onSettled?: () => void;
}) {
  const [pending, setPending] = useState<PendingPairing[] | null>(null);
  const [identities, setIdentities] = useState<ChannelIdentity[] | null>(null);
  const [pendingError, setPendingError] = useState<string | null>(null);
  const [identitiesError, setIdentitiesError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [forms, setForms] = useState<Record<string, SettleForm>>({});
  const [confirming, setConfirming] = useState<string | null>(null);
  const { busyRow, rowErrors, rowNotes, setRowError, setRowNote, act } = useRowActions();

  const loadPending = useCallback(async () => {
    try {
      const res = await fetch(`${BRIDGE}/api/channels/${encodeURIComponent(channel)}/pending`);
      if (!res.ok) {
        setPendingError(await readBridgeReply(res));
        return;
      }
      const body = (await res.json()) as { pending?: PendingPairing[] };
      setPending(Array.isArray(body.pending) ? body.pending : []);
      setPendingError(null);
    } catch {
      setPendingError("The dashboard could not reach the bridge to read who is waiting.");
    }
  }, [channel]);

  const loadIdentities = useCallback(async () => {
    try {
      const res = await fetch(`${BRIDGE}/api/channels/${encodeURIComponent(channel)}/identities`);
      if (!res.ok) {
        setIdentitiesError(await readBridgeReply(res));
        return;
      }
      const body = (await res.json()) as { identities?: ChannelIdentity[] };
      setIdentities(Array.isArray(body.identities) ? body.identities : []);
      setIdentitiesError(null);
    } catch {
      setIdentitiesError("The dashboard could not reach the bridge to read the bindings.");
    }
  }, [channel]);

  useEffect(() => {
    let live = true;
    void (async () => {
      await Promise.all([loadPending(), loadIdentities()]);
      if (live) setLoading(false);
    })();
    return () => {
      live = false;
    };
  }, [loadPending, loadIdentities]);

  const BLANK_FORM: SettleForm = { code: "", email: "", role: DEFAULT_PAIRED_ROLE };

  const formFor = (id: string): SettleForm => forms[id] ?? BLANK_FORM;

  /**
   * Edits the form from the updater's own `prev`, never from the render-scope
   * `forms`. Reading the closure would lose the first of two updates batched
   * into one render — not reachable through the fields below today, and exactly
   * the shape that becomes a bug the day somebody adds a paste handler that
   * fills the code and the address together.
   */
  const editForm = (id: string, patch: Partial<SettleForm>) =>
    setForms((prev) => ({ ...prev, [id]: { ...(prev[id] ?? BLANK_FORM), ...patch } }));

  const openForm = (id: string) => {
    setConfirming(null);
    setRowError(id, null);
    setRowNote(id, null);
    setForms((prev) => ({ ...prev, [id]: prev[id] ?? BLANK_FORM }));
  };

  const closeForm = (id: string) =>
    setForms((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });

  /** The alphabet is upper case, so the operator's lower case is not a refusal. */
  const codeOf = (id: string) => formFor(id).code.trim().toUpperCase();

  async function approve(row: PendingPairing) {
    const form = formFor(row.id);
    const code = codeOf(row.id);
    if (code.length !== CODE_LENGTH || !form.email.trim()) return;
    await act(
      row.id,
      `${BRIDGE}/api/channels/${encodeURIComponent(channel)}/pairings/${encodeURIComponent(code)}/approve`,
      { method: "POST", body: JSON.stringify({ email: form.email.trim(), role: form.role }) },
      () => {
        closeForm(row.id);
        setRowNote(row.id, "Bound. The sender can now reach this instance.");
        void loadPending();
        void loadIdentities();
        onSettled?.();
      }
    );
  }

  async function deny(row: PendingPairing) {
    const code = codeOf(row.id);
    if (code.length !== CODE_LENGTH) return;
    await act(
      row.id,
      `${BRIDGE}/api/channels/${encodeURIComponent(channel)}/pairings/${encodeURIComponent(code)}/deny`,
      { method: "POST" },
      () => {
        closeForm(row.id);
        setRowNote(row.id, "Denied. The code is spent and cannot be used again.");
        void loadPending();
        onSettled?.();
      }
    );
  }

  async function revoke(identity: ChannelIdentity) {
    await act(
      identity.id,
      `${BRIDGE}/api/channels/${encodeURIComponent(channel)}/identities/${encodeURIComponent(identity.id)}`,
      { method: "DELETE" },
      () => {
        setConfirming(null);
        setRowNote(identity.id, "Binding removed. The same sender may pair again.");
        void loadIdentities();
      }
    );
  }

  return (
    <div
      className="flex flex-col gap-4 rounded-md border border-border bg-background/60 p-3"
      data-testid={`channel-access-${channel}`}
    >
      {loading ? (
        <p
          className="flex items-center gap-2 text-xs text-muted-foreground"
          data-testid={`channel-access-loading-${channel}`}
        >
          <Loader2 aria-hidden className="size-3.5 animate-spin" />
          Reading who is waiting and who is bound…
        </p>
      ) : null}

      <section className="flex flex-col gap-2">
        <h4 className="text-xs font-medium uppercase tracking-[0.08em] text-muted-foreground">
          Pending pairings
        </h4>
        <p className="text-[11px] text-muted-foreground" data-testid={`channel-pending-hint-${channel}`}>
          The six-character code is never returned by the appliance — not even to an operator — so
          ask the sender for the code they were given, and check it is the person you think it is
          before you bind them.
        </p>

        {pendingError ? (
          <p className="text-xs text-destructive" data-testid={`channel-pending-error-${channel}`}>
            {pendingError}
          </p>
        ) : null}

        {!pendingError && pending && pending.length === 0 ? (
          <p className="text-xs text-muted-foreground" data-testid={`channel-pending-empty-${channel}`}>
            Nobody is waiting on a decision.
          </p>
        ) : null}

        {(pending ?? []).map((row) => {
          const open = Object.prototype.hasOwnProperty.call(forms, row.id);
          const form = formFor(row.id);
          const code = codeOf(row.id);
          return (
            <div
              key={row.id}
              data-testid={`channel-pending-${row.id}`}
              className="flex flex-col gap-2 rounded-md border border-border bg-card p-2.5"
            >
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <Badge variant="outline" className="border-warning/30 bg-warning/10 text-warning">
                  waiting
                </Badge>
                <span className="text-foreground">
                  {row.display_name_present
                    ? "A sender who gave a display name"
                    : "A sender who gave no display name"}
                </span>
                <span className="text-muted-foreground">asked at {when(row.created_at)}</span>
                <span className="text-muted-foreground">· code dies {when(row.expires_at)}</span>
              </div>

              {open ? (
                <form
                  data-testid={`pairing-form-${row.id}`}
                  className="flex flex-col gap-2"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void approve(row);
                  }}
                >
                  <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-end">
                    <label className="flex flex-col gap-1 text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
                      Pairing code
                      <Input
                        data-testid={`pairing-code-${row.id}`}
                        autoComplete="off"
                        spellCheck={false}
                        maxLength={CODE_LENGTH}
                        placeholder="6 characters"
                        className="w-full font-mono uppercase sm:w-36"
                        value={form.code}
                        onChange={(event) => editForm(row.id, { code: event.target.value })}
                      />
                    </label>
                    <label className="flex flex-col gap-1 text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
                      Bind to
                      <Input
                        data-testid={`pairing-email-${row.id}`}
                        type="email"
                        autoComplete="off"
                        placeholder="alice@example.com"
                        className="w-full sm:w-64"
                        value={form.email}
                        onChange={(event) => editForm(row.id, { email: event.target.value })}
                      />
                    </label>
                    <label className="flex flex-col gap-1 text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
                      Role
                      <NativeSelect
                        data-testid={`pairing-role-${row.id}`}
                        value={form.role}
                        onChange={(event) => editForm(row.id, { role: event.target.value })}
                      >
                        {PAIRABLE_ROLES.map((role) => (
                          <option key={role} value={role}>
                            {role}
                          </option>
                        ))}
                      </NativeSelect>
                    </label>
                  </div>
                  <p className="text-[11px] text-muted-foreground">
                    Pairing grants <span className="font-mono">viewer</span> or{" "}
                    <span className="font-mono">member</span> only. An operator role is never
                    reachable from a message a stranger sent.
                  </p>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <Button
                      type="submit"
                      size="xs"
                      disabled={
                        busyRow === row.id || code.length !== CODE_LENGTH || !form.email.trim()
                      }
                      data-testid={`pairing-approve-${row.id}`}
                    >
                      {busyRow === row.id ? <Loader2 aria-hidden className="animate-spin" /> : null}
                      Approve
                    </Button>
                    <Button
                      type="button"
                      variant="destructive"
                      size="xs"
                      disabled={busyRow === row.id || code.length !== CODE_LENGTH}
                      data-testid={`pairing-deny-${row.id}`}
                      onClick={() => void deny(row)}
                    >
                      Deny
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="xs"
                      data-testid={`pairing-cancel-${row.id}`}
                      onClick={() => closeForm(row.id)}
                    >
                      Cancel
                    </Button>
                  </div>
                </form>
              ) : (
                <div>
                  <Button
                    variant="outline"
                    size="xs"
                    data-testid={`pairing-settle-${row.id}`}
                    onClick={() => openForm(row.id)}
                  >
                    Approve or deny
                  </Button>
                </div>
              )}

              {rowErrors[row.id] ? (
                <p className="text-xs text-destructive" data-testid={`pairing-error-${row.id}`}>
                  {rowErrors[row.id]}
                </p>
              ) : null}
              {rowNotes[row.id] ? (
                <p className="text-xs text-success" data-testid={`pairing-note-${row.id}`}>
                  {rowNotes[row.id]}
                </p>
              ) : null}
            </div>
          );
        })}
      </section>

      <section className="flex flex-col gap-2">
        <h4 className="text-xs font-medium uppercase tracking-[0.08em] text-muted-foreground">
          Bound identities
        </h4>

        {identitiesError ? (
          <p className="text-xs text-destructive" data-testid={`channel-identities-error-${channel}`}>
            {identitiesError}
          </p>
        ) : null}

        {!identitiesError && identities && identities.length === 0 ? (
          <p
            className="text-xs text-muted-foreground"
            data-testid={`channel-identities-empty-${channel}`}
          >
            Nobody is bound to this channel.
          </p>
        ) : null}

        {(identities ?? []).map((identity) => (
          <div
            key={identity.id}
            data-testid={`channel-identity-${identity.id}`}
            className="flex flex-col gap-2 rounded-md border border-border bg-card p-2.5"
          >
            <div className="flex flex-wrap items-center gap-2 text-xs">
              {/*
                `min-w-0 break-words` on every span that carries a value the
                SERVER chose: a flex item's default `min-width: auto` refuses to
                shrink below its longest word, so a display name a workspace
                admin set to 120 characters pushes the settings pane sideways at
                390 px instead of wrapping.
              */}
              <span className="min-w-0 break-words font-medium text-foreground">
                {identity.display_name || identity.user_id || "an unnamed binding"}
              </span>
              <Badge variant="outline" className="text-muted-foreground">
                {identity.role || "role unknown"}
              </Badge>
              <span className="min-w-0 break-all font-mono text-[11px] text-muted-foreground">
                {identity.native_id_fingerprint || "no fingerprint reported"}
              </span>
              <span className="text-muted-foreground">paired {when(identity.paired_at)}</span>
              <Button
                variant="ghost"
                size="icon-xs"
                className="ml-auto"
                aria-label={`Remove the binding for ${identity.display_name || identity.user_id || "this sender"}`}
                data-testid={`identity-remove-${identity.id}`}
                onClick={() => setConfirming(identity.id)}
              >
                <Trash2 aria-hidden />
              </Button>
            </div>

            {confirming === identity.id ? (
              <div
                role="alertdialog"
                aria-live="assertive"
                aria-label="Remove this binding?"
                className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-background px-2 py-1.5"
              >
                <span className="text-xs text-foreground">
                  Remove this binding? The sender loses access at once and may pair again later.
                </span>
                <Button
                  variant="destructive"
                  size="xs"
                  disabled={busyRow === identity.id}
                  data-testid={`identity-remove-confirm-${identity.id}`}
                  onClick={() => void revoke(identity)}
                >
                  Remove binding
                </Button>
                <Button
                  variant="ghost"
                  size="xs"
                  data-testid={`identity-remove-cancel-${identity.id}`}
                  onClick={() => setConfirming(null)}
                >
                  Keep it
                </Button>
              </div>
            ) : null}

            {rowErrors[identity.id] ? (
              <p className="text-xs text-destructive" data-testid={`identity-error-${identity.id}`}>
                {rowErrors[identity.id]}
              </p>
            ) : null}
            {rowNotes[identity.id] ? (
              <p className="text-xs text-success" data-testid={`identity-note-${identity.id}`}>
                {rowNotes[identity.id]}
              </p>
            ) : null}
          </div>
        ))}
      </section>
    </div>
  );
}
