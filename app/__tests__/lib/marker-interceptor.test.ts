import { describe, it, expect } from "vitest";
import { MarkerInterceptor } from "@/lib/gateway/marker-interceptor";

describe("MarkerInterceptor", () => {
  it("passes through plain text unchanged", () => {
    const interceptor = new MarkerInterceptor();
    const result = interceptor.addChunk("Hello, how are you?");
    expect(result.text).toBe("Hello, how are you?");
    expect(result.markers).toHaveLength(0);
  });

  it("extracts a complete DASHBOARD marker", () => {
    const interceptor = new MarkerInterceptor();
    const result = interceptor.addChunk(
      'Here are your contacts. [DASHBOARD:{"intent":"contacts"}] Let me know if you need more.'
    );
    expect(result.text).toBe(
      "Here are your contacts.  Let me know if you need more."
    );
    expect(result.markers).toHaveLength(1);
    expect(result.markers[0]).toEqual({
      type: "dashboard",
      intent: "contacts",
      data: undefined,
    });
  });

  it("extracts DASHBOARD marker with nested data", () => {
    const interceptor = new MarkerInterceptor();
    const result = interceptor.addChunk(
      '[DASHBOARD:{"intent":"custom","data":{"description":"show health","items":["a","b"]}}]'
    );
    expect(result.text).toBe("");
    expect(result.markers).toHaveLength(1);
    expect(result.markers[0]).toEqual({
      type: "dashboard",
      intent: "custom",
      data: { description: "show health", items: ["a", "b"] },
    });
  });

  it("extracts a RENDER marker", () => {
    const interceptor = new MarkerInterceptor();
    const result = interceptor.addChunk(
      'Check this out. [RENDER:render_contact_table:{"search":"john"}]'
    );
    expect(result.text).toBe("Check this out. ");
    expect(result.markers).toHaveLength(1);
    expect(result.markers[0]).toEqual({
      type: "render",
      component: "render_contact_table",
      props: { search: "john" },
    });
  });

  it("buffers partial DASHBOARD marker across chunks", () => {
    const interceptor = new MarkerInterceptor();

    // First chunk: text + start of marker
    const r1 = interceptor.addChunk('Here are contacts. [DASHBOARD:{"int');
    expect(r1.text).toBe("Here are contacts. ");
    expect(r1.markers).toHaveLength(0);

    // Second chunk: rest of marker
    const r2 = interceptor.addChunk('ent":"contacts"}]');
    expect(r2.text).toBe("");
    expect(r2.markers).toHaveLength(1);
    expect(r2.markers[0]).toEqual({
      type: "dashboard",
      intent: "contacts",
      data: undefined,
    });
  });

  it("buffers partial prefix character by character", () => {
    const interceptor = new MarkerInterceptor();

    const r1 = interceptor.addChunk("Hello [");
    expect(r1.text).toBe("Hello ");
    expect(r1.markers).toHaveLength(0);

    const r2 = interceptor.addChunk("D");
    expect(r2.text).toBe("");

    const r3 = interceptor.addChunk("A");
    expect(r3.text).toBe("");

    const r4 = interceptor.addChunk('SHBOARD:{"intent":"health"}]');
    expect(r4.text).toBe("");
    expect(r4.markers).toHaveLength(1);
    expect(r4.markers[0].type).toBe("dashboard");
  });

  it("handles deeply nested JSON in marker", () => {
    const interceptor = new MarkerInterceptor();
    const marker =
      '[DASHBOARD:{"intent":"custom","data":{"nested":{"deep":{"value":42}},"arr":[1,2,3]}}]';
    const result = interceptor.addChunk(`Text before. ${marker} Text after.`);
    expect(result.text).toBe("Text before.  Text after.");
    expect(result.markers).toHaveLength(1);
    expect(result.markers[0].type).toBe("dashboard");
    const m = result.markers[0] as { data: { nested: { deep: { value: number } } } };
    expect(m.data.nested.deep.value).toBe(42);
  });

  it("flush emits buffered partial as text", () => {
    const interceptor = new MarkerInterceptor();

    interceptor.addChunk("Hello [DASHBOARD:");
    const flushed = interceptor.flush();
    expect(flushed.text).toBe("[DASHBOARD:");
    expect(flushed.markers).toHaveLength(0);
  });

  it("handles malformed JSON in DASHBOARD marker", () => {
    const interceptor = new MarkerInterceptor();
    const result = interceptor.addChunk("[DASHBOARD:{invalid json}]");
    // Malformed marker gets emitted as text
    expect(result.text).toBe("[DASHBOARD:{invalid json}]");
    expect(result.markers).toHaveLength(0);
  });

  it("handles [ that is not a marker prefix", () => {
    const interceptor = new MarkerInterceptor();
    const result = interceptor.addChunk("Array [1, 2, 3] values");
    expect(result.text).toBe("Array [1, 2, 3] values");
    expect(result.markers).toHaveLength(0);
  });

  it("handles multiple markers in one chunk", () => {
    const interceptor = new MarkerInterceptor();
    const result = interceptor.addChunk(
      'First [DASHBOARD:{"intent":"contacts"}] then [DASHBOARD:{"intent":"health"}] done.'
    );
    expect(result.text).toBe("First  then  done.");
    expect(result.markers).toHaveLength(2);
    expect(result.markers[0]).toMatchObject({ intent: "contacts" });
    expect(result.markers[1]).toMatchObject({ intent: "health" });
  });

  it("handles DASHBOARD marker with strings containing braces", () => {
    const interceptor = new MarkerInterceptor();
    const result = interceptor.addChunk(
      '[DASHBOARD:{"intent":"custom","data":{"desc":"show {things} in curly braces"}}]'
    );
    expect(result.text).toBe("");
    expect(result.markers).toHaveLength(1);
    expect(result.markers[0].type).toBe("dashboard");
  });

  it("correctly handles text after marker in streaming", () => {
    const interceptor = new MarkerInterceptor();

    const r1 = interceptor.addChunk('[DASHBOARD:{"intent":"contacts"}] Here');
    expect(r1.markers).toHaveLength(1);
    expect(r1.text).toBe(" Here");

    const r2 = interceptor.addChunk(" is your data.");
    expect(r2.text).toBe(" is your data.");
    expect(r2.markers).toHaveLength(0);
  });

  it("handles empty chunks", () => {
    const interceptor = new MarkerInterceptor();
    const result = interceptor.addChunk("");
    expect(result.text).toBe("");
    expect(result.markers).toHaveLength(0);
  });

  it("handles RENDER marker with nested props", () => {
    const interceptor = new MarkerInterceptor();
    const result = interceptor.addChunk(
      '[RENDER:render_data_table:{"columns":["name","email"],"rows":[{"name":"John","email":"j@x.com"}]}]'
    );
    expect(result.text).toBe("");
    expect(result.markers).toHaveLength(1);
    expect(result.markers[0]).toMatchObject({
      type: "render",
      component: "render_data_table",
    });
  });

  it("buffers [ at end of chunk that could be marker start", () => {
    const interceptor = new MarkerInterceptor();

    const r1 = interceptor.addChunk("Some text [");
    expect(r1.text).toBe("Some text ");

    // Now send something that is NOT a marker
    const r2 = interceptor.addChunk("not a marker]");
    // Should flush the buffered [ since it wasn't a marker prefix
    expect(r2.text).toBe("[not a marker]");
    expect(r2.markers).toHaveLength(0);
  });
});
