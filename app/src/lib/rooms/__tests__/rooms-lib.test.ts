/**
 * The small rules the rooms UI leans on: what a room id looks like, where a
 * room's link goes, what each access level may do, which edition blocks the
 * dashboard will draw, and the one place the user-facing name lives.
 */
import { describe, expect, it } from "vitest";

import { isRoomId, roomLink } from "@/lib/rooms/room-id";
import { can } from "@/lib/rooms/access";
import { EDITION_COMPONENTS, isRenderableBlock } from "@/lib/rooms/edition-blocks";
import { ROOM_LABEL, ROOM_GROUP_LABELS } from "@/lib/rooms/labels";
import { hasComponent } from "@/lib/component-registry";
import { REPORT_ID } from "./fixtures";

describe("room ids", () => {
  it("accepts a UUID and nothing else", () => {
    expect(isRoomId(REPORT_ID)).toBe(true);
    expect(isRoomId(REPORT_ID.toUpperCase())).toBe(true);
    for (const bad of ["", "main", "agent:main:room:x", `${REPORT_ID}x`, " " + REPORT_ID, 42, null, undefined]) {
      expect(isRoomId(bad)).toBe(false);
    }
  });

  it("links to the chat view with the room parameter, as every room tool result does", () => {
    expect(roomLink(REPORT_ID)).toBe(`/?v=chat&room=${REPORT_ID}`);
  });
});

describe("room access", () => {
  it("lets a viewer read and nothing else", () => {
    expect(can("viewer", "read")).toBe(true);
    for (const action of ["post", "run", "request_period", "accept_task", "duplicate", "edit_definition", "share"] as const) {
      expect(can("viewer", action)).toBe(false);
    }
  });

  it("lets a shared member post and run but not edit or share", () => {
    expect(can("shared", "post")).toBe(true);
    expect(can("shared", "run")).toBe(true);
    expect(can("shared", "request_period")).toBe(true);
    expect(can("shared", "edit_definition")).toBe(false);
    expect(can("shared", "share")).toBe(false);
  });

  it("lets an assigned member manage the definition, and an admin reassign", () => {
    expect(can("assigned", "edit_definition")).toBe(true);
    expect(can("assigned", "share")).toBe(true);
    expect(can("assigned", "reassign")).toBe(false);
    expect(can("admin", "reassign")).toBe(true);
    expect(can("admin", "list_all")).toBe(true);
  });

  it("refuses everything for an unknown access level", () => {
    expect(can("none" as never, "read")).toBe(false);
    expect(can(undefined as never, "read")).toBe(false);
  });
});

describe("edition blocks", () => {
  it("draws only components the dashboard's registry has", () => {
    for (const name of EDITION_COMPONENTS) expect(hasComponent(name)).toBe(true);
  });

  it("is the contract's list", () => {
    expect([...EDITION_COMPONENTS].sort()).toEqual(
      [
        "render_bar_chart",
        "render_data_table",
        "render_line_chart",
        "render_markdown",
        "render_metric_card",
        "render_metric_grid",
        "render_pie_chart",
        "render_timeline",
      ].sort(),
    );
  });

  it("refuses a block outside the list or with the wrong shape", () => {
    expect(isRenderableBlock({ component: "render_bar_chart", props: {} })).toBe(true);
    expect(isRenderableBlock({ component: "render_form", props: {} })).toBe(false);
    expect(isRenderableBlock({ component: "render_bar_chart" })).toBe(false);
    expect(isRenderableBlock({ component: "render_bar_chart", props: [] })).toBe(false);
    expect(isRenderableBlock("render_bar_chart")).toBe(false);
    expect(isRenderableBlock(null)).toBe(false);
  });
});

describe("the user-facing name", () => {
  it("says Workspaces, from one place", () => {
    expect(ROOM_LABEL.plural).toBe("Workspaces");
    expect(ROOM_LABEL.singular).toBe("workspace");
    expect(ROOM_GROUP_LABELS).toEqual({ report: "Reports", agent: "Agents", task: "Tasks" });
  });
});
