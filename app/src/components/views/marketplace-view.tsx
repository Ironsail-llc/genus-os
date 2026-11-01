"use client";

import { useState, useEffect, useCallback } from "react";
import { ExternalLink, Download, Trash2, RefreshCw, Package } from "lucide-react";

import { TrustBadge } from "@/components/plugins/trust-badge";
import { useRuntimeConfig } from "@/components/runtime-config";
import { agentTrust, pluginTrust, type Trust } from "@/lib/plugins/trust";

interface InstalledAgent {
  agent_id: string;
  version: string;
  installed_at: string;
  source: string;
  department: string;
  has_manifest: boolean;
  /** The sha256 the bundle was pinned to, when the install recorded one. */
  source_sha256?: string;
  /** The distribution a plugin-shipped agent came in. */
  plugin?: string;
}

interface MarketplaceViewProps {
  visible: boolean;
}

// Browser calls stay on the authenticated same-origin BFF. The BFF alone owns
// the internal Bridge address and exchanges the signed-in caller's token.
const BRIDGE_URL = "/api/bridge";

export function MarketplaceView({ visible }: MarketplaceViewProps) {
  const { marketplaceUrl } = useRuntimeConfig();
  const [agents, setAgents] = useState<InstalledAgent[]>([]);
  /** Plugin trust by distribution name; empty when `/api/plugins` refused. */
  const [pluginTrustByName, setPluginTrustByName] = useState<Record<string, Trust>>({});
  const [loading, setLoading] = useState(false);
  const [installSlug, setInstallSlug] = useState("");
  const [installing, setInstalling] = useState(false);
  const [message, setMessage] = useState<{ text: string; type: "success" | "error" } | null>(null);

  /**
   * Operator-only on the bridge. A refusal or an older bridge leaves the map
   * empty, and a plugin-shipped agent then reads Unsigned rather than vanishing.
   */
  const fetchPluginTrust = useCallback(async () => {
    try {
      const res = await fetch(`${BRIDGE_URL}/api/plugins`);
      if (!res.ok) {
        setPluginTrustByName({});
        return;
      }
      const data = await res.json();
      const byName: Record<string, Trust> = {};
      for (const row of Array.isArray(data?.plugins) ? data.plugins : []) {
        if (row && typeof row.name === "string") byName[row.name] = pluginTrust(row);
      }
      setPluginTrustByName(byName);
    } catch {
      setPluginTrustByName({});
    }
  }, []);

  /** A plugin-shipped agent takes its plugin's trust; any other is its own. */
  const trustFor = (agent: InstalledAgent): Trust =>
    (agent.plugin ? pluginTrustByName[agent.plugin] : undefined) ?? agentTrust(agent);

  const fetchAgents = useCallback(async () => {
    setLoading(true);
    void fetchPluginTrust();
    try {
      const res = await fetch(`${BRIDGE_URL}/api/installed-agents`);
      if (res.ok) {
        const data = await res.json();
        setAgents(data.agents || []);
      }
    } catch (e) {
      console.error("Failed to fetch installed agents:", e);
    } finally {
      setLoading(false);
    }
  }, [fetchPluginTrust]);

  useEffect(() => {
    if (visible) fetchAgents();
  }, [visible, fetchAgents]);

  const handleInstall = async () => {
    if (!installSlug.trim()) return;
    setInstalling(true);
    setMessage(null);
    try {
      const res = await fetch(`${BRIDGE_URL}/api/installed-agents/install`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug: installSlug.trim() }),
      });
      if (res.ok) {
        const data = await res.json();
        setMessage({ text: `Installed ${data.agent_id}`, type: "success" });
        setInstallSlug("");
        fetchAgents();
      } else {
        const err = await res.json();
        setMessage({ text: err.detail || "Install failed", type: "error" });
      }
    } catch {
      setMessage({ text: "Install failed: network error", type: "error" });
    } finally {
      setInstalling(false);
    }
  };

  const handleRemove = async (agentId: string) => {
    try {
      const res = await fetch(`${BRIDGE_URL}/api/installed-agents/${agentId}`, {
        method: "DELETE",
      });
      if (res.ok) {
        setMessage({ text: `Removed ${agentId}`, type: "success" });
        fetchAgents();
      }
    } catch (e) {
      setMessage({ text: `Remove failed: ${e}`, type: "error" });
    }
  };

  const handleUpdate = async (agentId: string) => {
    try {
      const res = await fetch(`${BRIDGE_URL}/api/installed-agents/${agentId}/update`, {
        method: "POST",
      });
      if (res.ok) {
        setMessage({ text: `Updated ${agentId}`, type: "success" });
        fetchAgents();
      }
    } catch (e) {
      setMessage({ text: `Update failed: ${e}`, type: "error" });
    }
  };

  return (
    <div
      className="h-full w-full flex flex-col overflow-y-auto"
      style={{ display: visible ? "flex" : "none" }}
      data-testid="marketplace-view"
    >
      <div className="p-4 space-y-4">
        {/* Header */}
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">Marketplace</h2>
          <a
            href={`${marketplaceUrl}/browse`}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 text-sm text-info hover:text-info/80"
          >
            Browse Marketplace <ExternalLink className="w-3.5 h-3.5" />
          </a>
        </div>

        {/* Install bar */}
        <div className="flex gap-2">
          <input
            type="text"
            placeholder="Enter bundle slug or URL..."
            value={installSlug}
            onChange={(e) => setInstallSlug(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleInstall()}
            className="flex-1 px-3 py-1.5 text-sm rounded-md border border-input bg-background"
          />
          <button
            onClick={handleInstall}
            disabled={installing || !installSlug.trim()}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            <Download className="w-3.5 h-3.5" />
            {installing ? "Installing..." : "Install"}
          </button>
        </div>

        {/* Status message */}
        {message && (
          <div
            className={`text-sm px-3 py-2 rounded-md ${
              message.type === "success"
                ? "bg-success/10 text-success"
                : "bg-destructive/10 text-destructive"
            }`}
          >
            {message.text}
          </div>
        )}

        {/* Installed agents grid */}
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium text-muted-foreground">
              Installed Agents ({agents.length})
            </h3>
            <button
              onClick={fetchAgents}
              disabled={loading}
              className="p-1 rounded hover:bg-accent"
              title="Refresh"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
            </button>
          </div>

          {agents.length === 0 && !loading && (
            <div className="text-center py-8 text-muted-foreground text-sm">
              <Package className="w-8 h-8 mx-auto mb-2 opacity-40" />
              No agents installed from the marketplace yet.
            </div>
          )}

          <div className="grid gap-2">
            {agents.map((agent) => (
              <div
                key={agent.agent_id}
                className="flex items-center justify-between p-3 rounded-lg border border-border bg-card"
                data-testid={`installed-agent-${agent.agent_id}`}
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-sm truncate">{agent.agent_id}</span>
                    {agent.department && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-accent text-accent-foreground">
                        {agent.department}
                      </span>
                    )}
                    <span className="text-xs text-muted-foreground">v{agent.version}</span>
                    <TrustBadge
                      name={agent.agent_id}
                      trust={trustFor(agent)}
                      testId={`agent-trust-${agent.agent_id}`}
                    />
                  </div>
                  <div className="text-xs text-muted-foreground mt-0.5">
                    {agent.has_manifest ? "Active" : "Missing manifest"}
                    {agent.source && ` \u00b7 ${agent.source}`}
                  </div>
                </div>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => handleUpdate(agent.agent_id)}
                    className="p-1.5 rounded hover:bg-accent"
                    title="Update"
                  >
                    <RefreshCw className="w-3.5 h-3.5" />
                  </button>
                  <button
                    onClick={() => handleRemove(agent.agent_id)}
                    className="p-1.5 rounded hover:bg-destructive/10 text-destructive"
                    title="Remove"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                  <a
                    href={`${marketplaceUrl}/bundle/${agent.agent_id}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="p-1.5 rounded hover:bg-accent"
                    title="View on Marketplace"
                  >
                    <ExternalLink className="w-3.5 h-3.5" />
                  </a>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
