/**
 * "Root at home": the one session that may decide plugins for child tenants.
 *
 * The bridge's grant route is `require_root_operator` — the platform tenant's
 * owner or admin, NOT acting in a child. The page shows the per-child control
 * on exactly that condition, so a child admin, or a root admin who switched
 * into a child, is never offered a control whose only outcome is a 403.
 */
import { describe, expect, it } from "vitest";

import { isRootAtHome } from "@/lib/tenant-session";

describe("isRootAtHome", () => {
  it("is true when acting, home and root are the same tenant", () => {
    expect(
      isRootAtHome({ tenantId: "acme-parent", homeTenantId: "acme-parent", rootTenantId: "acme-parent" }),
    ).toBe(true);
  });

  it("is false for a root admin acting in a child", () => {
    expect(
      isRootAtHome({ tenantId: "acme-child-a", homeTenantId: "acme-parent", rootTenantId: "acme-parent" }),
    ).toBe(false);
  });

  it("is false for a child admin at home", () => {
    expect(
      isRootAtHome({ tenantId: "acme-child-a", homeTenantId: "acme-child-a", rootTenantId: "acme-parent" }),
    ).toBe(false);
  });

  it("is false when the session does not name the root", () => {
    expect(isRootAtHome({ tenantId: "acme-parent", homeTenantId: "acme-parent" })).toBe(false);
    expect(isRootAtHome(null)).toBe(false);
    expect(isRootAtHome(undefined)).toBe(false);
  });
});
