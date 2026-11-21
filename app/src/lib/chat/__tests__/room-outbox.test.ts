/**
 * A room's outbox: ONE ordered structure, ONE sender.
 *
 * Every room message gets its `seq` when it is typed and enters the outbox.
 * Exactly one sender drains it, head first, one message per turn; a refusal
 * (`room_busy`) leaves the head where it is. Tested as a property: for
 * generated interleavings of typing, the server answering ok or busy, the
 * room freeing up, the retry wait elapsing and the reader navigating away
 * and back, the server receives the messages in typing order, each once.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ROOM_BUSY_RETRY_MS,
  forgetRoomOutboxes,
  mergeRoomRows,
  pendingBubbles,
  roomOutbox,
  unsentBubbles,
  unsentShown,
  type OutboxItem,
  type RoomRow,
  type SendResult,
} from "@/lib/chat/room-outbox";

const ROOM = "11111111-1111-4111-8111-111111111111";

/** A small seeded generator, so a failing interleaving can be replayed. */
function prng(seed: number) {
  let s = seed >>> 0 || 1;
  return () => {
    s ^= s << 13;
    s ^= s >>> 17;
    s ^= s << 5;
    return (s >>> 0) / 0x100000000;
  };
}

type Pending = { item: OutboxItem; resolve: (r: SendResult) => void };

/** The server: every send attempt waits here until the test answers it. */
function server() {
  const pending: Pending[] = [];
  const received: string[] = [];
  const attempts: string[] = [];
  let maxInFlight = 0;
  const sender = (item: OutboxItem) =>
    new Promise<SendResult>((resolve) => {
      attempts.push(item.text);
      pending.push({
        item,
        resolve: (r) => {
          if (r.outcome === "ok") received.push(item.text);
          resolve(r);
        },
      });
      maxInFlight = Math.max(maxInFlight, pending.length);
    });
  return { pending, received, attempts, sender, maxInFlight: () => maxInFlight };
}

const settle = () => vi.advanceTimersByTimeAsync(0);

beforeEach(() => {
  vi.useFakeTimers();
  forgetRoomOutboxes();
});
afterEach(() => {
  vi.useRealTimers();
  forgetRoomOutboxes();
});

describe("the room outbox", () => {
  it("gives each message its seq when it is typed, and keeps typing order", () => {
    const box = roomOutbox(ROOM);
    const a = box.enqueue("A");
    const b = box.enqueue("B");
    expect(b.seq).toBeGreaterThan(a.seq);
    expect(box.state().items.map((i) => i.text)).toEqual(["A", "B"]);
  });

  it("the reviewer's sequence: B typed while A's first send is in flight, A refused, B still goes after A", async () => {
    const srv = server();
    const box = roomOutbox(ROOM);
    box.attach(srv.sender);
    box.enqueue("A");
    await settle();
    box.enqueue("B");
    await settle();
    expect(srv.attempts).toEqual(["A"]);
    srv.pending.shift()!.resolve({ outcome: "busy", answering: "Alice" });
    await settle();
    expect(box.state()).toMatchObject({ blocked: true, answering: "Alice" });
    expect(box.state().items.map((i) => i.text)).toEqual(["A", "B"]);
    box.roomFreed();
    await settle();
    srv.pending.shift()!.resolve({ outcome: "ok" });
    await settle();
    srv.pending.shift()!.resolve({ outcome: "ok" });
    await settle();
    expect(srv.received).toEqual(["A", "B"]);
    expect(srv.attempts).toEqual(["A", "A", "B"]);
  });

  it("retries a refused head after the wait, without being told", async () => {
    const srv = server();
    const box = roomOutbox(ROOM);
    box.attach(srv.sender);
    box.enqueue("A");
    await settle();
    srv.pending.shift()!.resolve({ outcome: "busy", answering: "" });
    await settle();
    expect(srv.attempts).toEqual(["A"]);
    await vi.advanceTimersByTimeAsync(ROOM_BUSY_RETRY_MS);
    expect(srv.attempts).toEqual(["A", "A"]);
  });

  it("drops a message whose send failed outright, and carries on with the next", async () => {
    const srv = server();
    const box = roomOutbox(ROOM);
    box.attach(srv.sender);
    box.enqueue("A");
    box.enqueue("B");
    await settle();
    srv.pending.shift()!.resolve({ outcome: "failed" });
    await settle();
    expect(srv.attempts).toEqual(["A", "B"]);
  });

  it("outlives the panel: what is queued when the reader leaves goes out, in order, when they return", async () => {
    const srv = server();
    const box = roomOutbox(ROOM);
    const detach = box.attach(srv.sender);
    box.enqueue("A");
    await settle();
    srv.pending.shift()!.resolve({ outcome: "busy", answering: "Alice" });
    box.enqueue("B");
    await settle();
    detach();
    box.roomFreed();
    await vi.advanceTimersByTimeAsync(ROOM_BUSY_RETRY_MS);
    expect(srv.attempts).toEqual(["A"]);
    roomOutbox(ROOM).attach(srv.sender);
    await settle();
    srv.pending.shift()!.resolve({ outcome: "ok" });
    await settle();
    srv.pending.shift()!.resolve({ outcome: "ok" });
    await settle();
    expect(srv.received).toEqual(["A", "B"]);
  });

  it("property: for generated interleavings, the server receives every message in typing order, each once", async () => {
    const ACTIONS = ["type", "type", "ok", "ok", "busy", "freed", "elapse", "navigate"] as const;
    for (let seed = 1; seed <= 300; seed++) {
      forgetRoomOutboxes();
      const rand = prng(seed);
      const srv = server();
      const typed: string[] = [];
      let detach = roomOutbox(ROOM).attach(srv.sender);
      const trace: string[] = [];
      const steps = 10 + Math.floor(rand() * 40);
      for (let step = 0; step < steps; step++) {
        const action = ACTIONS[Math.floor(rand() * ACTIONS.length)];
        trace.push(action);
        if (action === "type") {
          const text = `m${typed.length}`;
          typed.push(text);
          roomOutbox(ROOM).enqueue(text);
        } else if (action === "ok" || action === "busy") {
          srv.pending.shift()?.resolve(action === "ok" ? { outcome: "ok" } : { outcome: "busy", answering: "Bob" });
        } else if (action === "freed") {
          roomOutbox(ROOM).roomFreed();
        } else if (action === "elapse") {
          await vi.advanceTimersByTimeAsync(ROOM_BUSY_RETRY_MS);
        } else {
          // Away (the request in flight carries on) and back: a new panel, a new sender.
          detach();
          await settle();
          detach = roomOutbox(ROOM).attach(srv.sender);
        }
        await settle();
        const context = `seed ${seed}: ${trace.join(" ")}`;
        // One sender: never two sends in flight.
        expect(srv.pending.length, context).toBeLessThanOrEqual(1);
        // What arrived is a prefix of what was typed, in order, nothing twice.
        expect(srv.received, context).toEqual(typed.slice(0, srv.received.length));
        // The one attempt in flight is always the oldest message not yet received.
        if (srv.pending[0]) expect(srv.pending[0].item.text, context).toBe(typed[srv.received.length]);
      }
      // Then the room frees up for good: everything arrives, in order, once.
      for (let guard = 0; guard < typed.length * 3 + 5 && srv.received.length < typed.length; guard++) {
        roomOutbox(ROOM).roomFreed();
        await settle();
        srv.pending.shift()?.resolve({ outcome: "ok" });
        await settle();
      }
      expect(srv.received, `seed ${seed}: ${trace.join(" ")}`).toEqual(typed);
      expect(srv.maxInFlight()).toBeLessThanOrEqual(1);
      detach();
    }
  });

  it("gives each message a client id, which is the bubble's key", () => {
    const item = roomOutbox(ROOM).enqueue("A");
    expect(item.id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
  });
});

describe("reconciling a room's transcript with its outbox", () => {
  const local = (id: string, content: string): RoomRow => ({ id, role: "user", content });
  const stored = (n: number, content: string, clientId?: string): RoomRow => ({
    id: `hist-${n}`, role: "user", content, serverId: n, ...(clientId ? { clientId } : {}),
  });

  it("replaces the local bubble with the stored row that names its client id, in place", () => {
    const out = mergeRoomRows([local("c-1", "A"), local("c-2", "B")], [stored(10, "A", "c-1")]);
    expect(out.map((r) => r.id)).toEqual(["hist-10", "c-2"]);
  });

  it("matches by client id, never by text, when the stored row carries one", () => {
    const out = mergeRoomRows([local("c-1", "same")], [stored(10, "same", "c-other")]);
    expect(out.map((r) => r.id)).toEqual(["c-1", "hist-10"]);
  });

  it("never draws a bubble for a send another panel has in flight", () => {
    const box = roomOutbox(ROOM);
    box.attach(() => new Promise<SendResult>(() => {}));
    const a = box.enqueue("A");
    const b = box.enqueue("B");
    expect(box.state().sending?.id).toBe(a.id);
    expect(pendingBubbles(box.state(), [], new Set()).map((i) => i.id)).toEqual([b.id]);
    expect(pendingBubbles(box.state(), [], new Set([a.id])).map((i) => i.id)).toEqual([a.id, b.id]);
    expect(pendingBubbles(box.state(), [stored(1, "A", a.id)], new Set([a.id])).map((i) => i.id)).toEqual([b.id]);
  });

  for (const echo of [true, false]) {
    it(`property: leaving during a send and coming back never shows a message twice (engine ${echo ? "echoes" : "does not echo"} the client id)`, async () => {
      const ACTIONS = ["type", "type", "ok", "ok", "busy", "freed", "elapse", "navigate", "history", "history"] as const;
      for (let seed = 1; seed <= 300; seed++) {
        forgetRoomOutboxes();
        const rand = prng(seed * 7 + (echo ? 1 : 0));
        const typed: string[] = [];
        const storedRows: RoomRow[] = [];
        const pending: Pending[] = [];
        // One panel at a time: what it has drawn, and which sends it started.
        let rendered: RoomRow[] = [];
        let startedHere = new Set<string>();
        let mounted = true;
        const makeSender = (mine: Set<string>) => (item: OutboxItem) =>
          new Promise<SendResult>((resolve) => {
            mine.add(item.id);
            pending.push({
              item,
              resolve: (r) => {
                if (r.outcome === "ok") {
                  const n = storedRows.length + 1;
                  storedRows.push(stored(n, item.text, echo ? item.id : undefined));
                }
                resolve(r);
              },
            });
          });
        let detach = roomOutbox(ROOM).attach(makeSender(startedHere));
        const draw = () => {
          const merged = mergeRoomRows(rendered, storedRows);
          const extra = pendingBubbles(roomOutbox(ROOM).state(), merged, startedHere).map((i) => local(i.id, i.text));
          rendered = [...merged, ...extra];
        };
        const trace: string[] = [];
        const steps = 10 + Math.floor(rand() * 40);
        for (let step = 0; step < steps; step++) {
          const action = ACTIONS[Math.floor(rand() * ACTIONS.length)];
          trace.push(action);
          if (action === "type") {
            const text = `m${typed.length}`;
            typed.push(text);
            const item = roomOutbox(ROOM).enqueue(text);
            if (mounted) rendered = [...rendered, local(item.id, text)];
          } else if (action === "ok" || action === "busy") {
            pending.shift()?.resolve(action === "ok" ? { outcome: "ok" } : { outcome: "busy", answering: "Bob" });
          } else if (action === "freed") {
            roomOutbox(ROOM).roomFreed();
          } else if (action === "elapse") {
            await vi.advanceTimersByTimeAsync(ROOM_BUSY_RETRY_MS);
          } else if (action === "navigate") {
            // Away, with whatever is in flight still in flight, and back: a
            // new panel that draws nothing until its history arrives.
            detach();
            rendered = [];
            startedHere = new Set();
            mounted = false;
          } else if (!mounted) {
            // The new panel's history arrives, then it becomes the sender.
            mounted = true;
            rendered = [];
            draw();
            detach = roomOutbox(ROOM).attach(makeSender(startedHere));
          } else {
            draw(); // a live room message: catch up
          }
          await settle();
          if (mounted) draw();
          const contents = rendered.map((r) => r.content);
          expect(new Set(contents).size, `seed ${seed}: ${trace.join(" ")} → ${contents.join(",")}`).toBe(contents.length);
        }
        // Everything delivered, a last catch-up: each message exactly once.
        if (!mounted) {
          mounted = true;
          rendered = [];
          detach = roomOutbox(ROOM).attach(makeSender(startedHere));
        }
        for (let guard = 0; guard < typed.length * 3 + 5 && storedRows.length < typed.length; guard++) {
          roomOutbox(ROOM).roomFreed();
          await settle();
          pending.shift()?.resolve({ outcome: "ok" });
          await settle();
        }
        draw();
        expect(rendered.map((r) => r.content).sort(), `seed ${seed}: ${trace.join(" ")}`).toEqual([...typed].sort());
        detach();
      }
    });
  }

  it("keeps a send that failed after its panel left, until a panel has shown it as not sent", async () => {
    let fail!: (r: SendResult) => void;
    const box = roomOutbox(ROOM);
    const detach = box.attach(() => new Promise<SendResult>((r) => (fail = r)));
    const a = box.enqueue("A");
    await settle();
    detach();
    fail({ outcome: "failed" });
    await settle();
    expect(box.state().items).toEqual([]);
    expect(box.state().unsent.map((i) => i.id)).toEqual([a.id]);
    box.acknowledge([a.id]);
    expect(box.state().unsent).toEqual([]);
  });

  it("does not keep a failed send whose own panel is still there to show the error", async () => {
    let fail!: (r: SendResult) => void;
    const box = roomOutbox(ROOM);
    box.attach(() => new Promise<SendResult>((r) => (fail = r)));
    box.enqueue("A");
    await settle();
    fail({ outcome: "failed" });
    await settle();
    expect(box.state().unsent).toEqual([]);
  });

  it("property: no message is ever lost silently, whatever fails while the reader is away", async () => {
    const ACTIONS = ["type", "type", "ok", "busy", "fail", "fail", "freed", "elapse", "navigate", "history", "history"] as const;
    for (let seed = 1; seed <= 300; seed++) {
      forgetRoomOutboxes();
      const rand = prng(seed * 13 + 5);
      const typed: string[] = [];
      const storedRows: RoomRow[] = [];
      const pending: Array<Pending & { by: Set<string> }> = [];
      // Messages a panel has shown as not sent (its own error, or the outbox's unsent list).
      const shownNotSent = new Set<string>();
      let rendered: RoomRow[] = [];
      let startedHere = new Set<string>();
      let mounted = true;
      const makeSender = (mine: Set<string>) => (item: OutboxItem) =>
        new Promise<SendResult>((resolve) => {
          mine.add(item.id);
          pending.push({
            item,
            by: mine,
            resolve: (r) => {
              if (r.outcome === "ok") storedRows.push(stored(storedRows.length + 1, item.text, item.id));
              resolve(r);
            },
          });
        });
      let detach = roomOutbox(ROOM).attach(makeSender(startedHere));
      const draw = () => {
        const box = roomOutbox(ROOM);
        const merged = mergeRoomRows(rendered, storedRows);
        const extra = pendingBubbles(box.state(), merged, startedHere).map((i) => local(i.id, i.text));
        rendered = [...merged, ...extra];
        // The panel draws what failed while no panel of its own could say so, then acknowledges it.
        const unsent = box.state().unsent;
        for (const item of unsent) {
          if (!rendered.some((r) => r.id === item.id)) rendered = [...rendered, { ...local(item.id, item.text), note: "Not sent" }];
          shownNotSent.add(item.text);
        }
        if (unsent.length) box.acknowledge(unsent.map((i) => i.id));
      };
      const trace: string[] = [];
      const steps = 10 + Math.floor(rand() * 40);
      for (let step = 0; step < steps; step++) {
        const action = ACTIONS[Math.floor(rand() * ACTIONS.length)];
        trace.push(action);
        if (action === "type") {
          const text = `m${typed.length}`;
          typed.push(text);
          const item = roomOutbox(ROOM).enqueue(text);
          if (mounted) rendered = [...rendered, local(item.id, text)];
        } else if (action === "ok" || action === "busy") {
          pending.shift()?.resolve(action === "ok" ? { outcome: "ok" } : { outcome: "busy", answering: "Bob" });
        } else if (action === "fail") {
          const p = pending.shift();
          // Its own panel, still open, shows the error itself.
          if (p && mounted && p.by === startedHere) shownNotSent.add(p.item.text);
          p?.resolve({ outcome: "failed" });
        } else if (action === "freed") {
          roomOutbox(ROOM).roomFreed();
        } else if (action === "elapse") {
          await vi.advanceTimersByTimeAsync(ROOM_BUSY_RETRY_MS);
        } else if (action === "navigate") {
          detach();
          rendered = [];
          startedHere = new Set();
          mounted = false;
        } else if (!mounted) {
          mounted = true;
          rendered = [];
          draw();
          detach = roomOutbox(ROOM).attach(makeSender(startedHere));
        } else {
          draw();
        }
        await settle();
        if (mounted) draw();
        const contents = rendered.map((r) => r.content);
        expect(new Set(contents).size, `seed ${seed}: ${trace.join(" ")}`).toBe(contents.length);
      }
      if (!mounted) {
        mounted = true;
        rendered = [];
        draw();
        detach = roomOutbox(ROOM).attach(makeSender(startedHere));
      }
      for (let guard = 0; guard < typed.length * 3 + 5 && (pending.length || roomOutbox(ROOM).state().items.length); guard++) {
        roomOutbox(ROOM).roomFreed();
        await settle();
        pending.shift()?.resolve({ outcome: "ok" });
        await settle();
      }
      draw();
      // Every message arrived, or a panel said it was not sent. None vanished.
      const arrived = new Set(storedRows.map((r) => r.content));
      const lost = typed.filter((t) => !arrived.has(t) && !shownNotSent.has(t));
      expect(lost, `seed ${seed}: ${trace.join(" ")}`).toEqual([]);
      detach();
    }
  });

  it("keeps whether a failed send was refused or its outcome is unknown", async () => {
    const answers: Array<(r: SendResult) => void> = [];
    const box = roomOutbox(ROOM);
    const detach = box.attach(() => new Promise<SendResult>((r) => answers.push(r)));
    const a = box.enqueue("A");
    await settle();
    detach();
    answers.shift()!({ outcome: "failed", rejected: false });
    await settle();
    expect(box.state().unsent).toEqual([expect.objectContaining({ id: a.id, rejected: false })]);
  });

  it("draws no unsent bubble for a message history already holds", () => {
    const box = roomOutbox(ROOM);
    const item = { id: "c-1", seq: 1, text: "A", rejected: false };
    const state = { ...box.state(), unsent: [item] };
    expect(unsentBubbles(state, [stored(5, "A", "c-1")]).map((i) => i.id)).toEqual([]);
    // No echo: a stored row with the same text and no client id stands for it.
    expect(unsentBubbles(state, [stored(5, "A")]).map((i) => i.id)).toEqual([]);
    // A stored row naming ANOTHER client id is another message.
    expect(unsentBubbles(state, [stored(5, "A", "c-9")]).map((i) => i.id)).toEqual(["c-1"]);
    expect(unsentBubbles(state, []).map((i) => i.id)).toEqual(["c-1"]);
  });

  it("still draws a refused entry when an earlier stored message has the same text", () => {
    const box = roomOutbox(ROOM);
    const refused = { id: "c-1", seq: 1, text: "thanks", rejected: true };
    const state = { ...box.state(), unsent: [refused] };
    // Refused before admission, so no stored row can be its copy: an earlier "thanks" is another message.
    expect(unsentBubbles(state, [stored(5, "thanks")]).map((i) => i.id)).toEqual(["c-1"]);
    expect(unsentShown(state, [stored(5, "thanks")])).toEqual([]);
    expect(unsentShown(state, [stored(5, "thanks"), local("c-1", "thanks")])).toEqual(["c-1"]);
  });

  it("counts an unknown-outcome entry as shown only when it is drawn or its stored copy is", () => {
    const box = roomOutbox(ROOM);
    const unknown = { id: "c-2", seq: 2, text: "run it", rejected: false };
    const state = { ...box.state(), unsent: [unknown] };
    expect(unsentShown(state, [])).toEqual([]);
    expect(unsentShown(state, [stored(7, "run it", "c-2")])).toEqual(["c-2"]);
    expect(unsentShown(state, [stored(7, "run it")])).toEqual(["c-2"]);
    expect(unsentShown(state, [local("c-2", "run it")])).toEqual(["c-2"]);
  });

  for (const echo of [true, false]) {
    it(`property: no refusal is hidden, no stored message is called not sent, with repeated texts (client id ${echo ? "echoed" : "not echoed"})`, async () => {
      const ACTIONS = ["type", "type", "ok", "busy", "refused", "failStored", "failLost", "navigate", "history", "history", "freed", "elapse"] as const;
      const WORDS = ["thanks", "yes", "run it again"];
      for (let seed = 1; seed <= 300; seed++) {
        forgetRoomOutboxes();
        const rand = prng(seed * 17 + (echo ? 3 : 0));
        // Every message by its outbox id: texts repeat, identities do not.
        const typed: Array<{ id: string; text: string }> = [];
        const storedRows: RoomRow[] = [];
        const storedIds = new Set<string>();
        const outcome = new Map<string, "refused" | "unknown">();
        const pending: Array<Pending & { by: Set<string> }> = [];
        // Drawn as not sent (or as possibly not sent), by id: only entries a bubble was drawn for.
        const told = new Set<string>();
        // Unknown outcomes taken for a stored row with their text (no echo to tell them apart).
        const matchedByText = new Set<string>();
        let rendered: RoomRow[] = [];
        let startedHere = new Set<string>();
        let mounted = true;
        const store = (item: OutboxItem) => {
          storedIds.add(item.id);
          storedRows.push(stored(storedRows.length + 1, item.text, echo ? item.id : undefined));
        };
        const makeSender = (mine: Set<string>) => (item: OutboxItem) =>
          new Promise<SendResult>((resolve) => {
            mine.add(item.id);
            pending.push({ item, by: mine, resolve });
          });
        let detach = roomOutbox(ROOM).attach(makeSender(startedHere));
        const draw = () => {
          const box = roomOutbox(ROOM);
          const merged = mergeRoomRows(rendered, storedRows);
          const state = box.state();
          const drawnUnsent = unsentBubbles(state, merged);
          const extra = [
            ...pendingBubbles(state, merged, startedHere).map((i) => local(i.id, i.text)),
            ...drawnUnsent.map((i) => ({ ...local(i.id, i.text), note: i.rejected ? "Not sent" : "May not have reached" })),
          ];
          rendered = [...merged, ...extra];
          for (const i of drawnUnsent) told.add(i.id);
          // Acknowledged: only what is now on screen, drawn or as its stored copy.
          const shown = unsentShown(state, rendered);
          for (const id of shown) if (!told.has(id) && !rendered.some((r) => r.id === id || r.clientId === id)) matchedByText.add(id);
          if (shown.length) box.acknowledge(shown);
        };
        const trace: string[] = [];
        const steps = 10 + Math.floor(rand() * 40);
        for (let step = 0; step < steps; step++) {
          const action = ACTIONS[Math.floor(rand() * ACTIONS.length)];
          trace.push(action);
          if (action === "type") {
            const text = WORDS[Math.floor(rand() * WORDS.length)];
            const item = roomOutbox(ROOM).enqueue(text);
            typed.push({ id: item.id, text });
            if (mounted) rendered = [...rendered, local(item.id, text)];
          } else if (action === "ok") {
            const p = pending.shift();
            if (p) {
              store(p.item);
              p.resolve({ outcome: "ok" });
            }
          } else if (action === "busy") {
            pending.shift()?.resolve({ outcome: "busy", answering: "Bob" });
          } else if (action === "refused" || action === "failStored" || action === "failLost") {
            const p = pending.shift();
            if (p) {
              // Admitted and stored, then the answer was lost (a 504, a cut stream).
              if (action === "failStored") store(p.item);
              outcome.set(p.item.id, action === "refused" ? "refused" : "unknown");
              // Its own panel, still open, shows the error itself.
              if (mounted && p.by === startedHere) told.add(p.item.id);
              p.resolve({ outcome: "failed", rejected: action === "refused" });
            }
          } else if (action === "freed") {
            roomOutbox(ROOM).roomFreed();
          } else if (action === "elapse") {
            await vi.advanceTimersByTimeAsync(ROOM_BUSY_RETRY_MS);
          } else if (action === "navigate") {
            detach();
            rendered = [];
            startedHere = new Set();
            mounted = false;
          } else if (!mounted) {
            mounted = true;
            rendered = [];
            draw();
            detach = roomOutbox(ROOM).attach(makeSender(startedHere));
          } else {
            draw();
          }
          await settle();
          if (mounted) draw();
          const context = `seed ${seed}: ${trace.join(" ")}`;
          // Never more copies of a text on screen than messages typed with it.
          for (const word of WORDS) {
            expect(rendered.filter((r) => r.content === word).length, context).toBeLessThanOrEqual(typed.filter((t) => t.text === word).length);
          }
          // A stored message is never called not sent.
          expect(rendered.filter((r) => r.note === "Not sent" && storedIds.has(r.id)), context).toEqual([]);
          // Only an unknown outcome is ever taken for a stored row by its text.
          expect([...matchedByText].filter((id) => outcome.get(id) === "refused"), context).toEqual([]);
        }
        if (!mounted) {
          mounted = true;
          rendered = [];
          draw();
          detach = roomOutbox(ROOM).attach(makeSender(startedHere));
        }
        for (let guard = 0; guard < typed.length * 3 + 5 && (pending.length || roomOutbox(ROOM).state().items.length); guard++) {
          roomOutbox(ROOM).roomFreed();
          await settle();
          const p = pending.shift();
          if (p) {
            store(p.item);
            p.resolve({ outcome: "ok" });
          }
          await settle();
          draw();
        }
        draw();
        // Every message arrived, or a bubble said it was not (or may not have been) sent, or,
        // with no echo, an unknown outcome stands as a stored row with its text. Nothing vanished.
        const lost = typed.filter((t) => !storedIds.has(t.id) && !told.has(t.id) && !matchedByText.has(t.id));
        expect(lost, `seed ${seed}: ${trace.join(" ")}`).toEqual([]);
        if (echo) expect([...matchedByText], `seed ${seed}`).toEqual([]);
        expect(roomOutbox(ROOM).state().unsent, `seed ${seed}`).toEqual([]);
        detach();
      }
    });
  }
});
