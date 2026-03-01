/**
 * Server-side marker interceptor for the chat SSE stream.
 *
 * Buffers streaming text to extract [DASHBOARD:{...}] and [RENDER:component:props]
 * markers BEFORE they reach the browser. Emits clean text and marker events separately.
 *
 * This prevents markers from appearing character-by-character in the chat UI.
 */

export interface DashboardMarkerEvent {
  type: "dashboard";
  intent: string;
  data?: Record<string, unknown>;
}

export interface RenderMarkerEvent {
  type: "render";
  component: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  props: Record<string, any>;
}

export type MarkerEvent = DashboardMarkerEvent | RenderMarkerEvent;

export interface InterceptResult {
  text: string;
  markers: MarkerEvent[];
}

const DASHBOARD_PREFIX = "[DASHBOARD:";
const RENDER_PREFIX = "[RENDER:";

export class MarkerInterceptor {
  private buffer = "";

  /** Process a new text chunk. Returns clean text and any extracted markers. */
  addChunk(chunk: string): InterceptResult {
    this.buffer += chunk;
    return this.extract(false);
  }

  /** Flush remaining buffer (call at end of stream). */
  flush(): InterceptResult {
    const result = this.extract(true);
    this.buffer = "";
    return result;
  }

  private extract(force: boolean): InterceptResult {
    let text = "";
    const markers: MarkerEvent[] = [];

    while (this.buffer.length > 0) {
      const dashIdx = this.buffer.indexOf(DASHBOARD_PREFIX);
      const renderIdx = this.buffer.indexOf(RENDER_PREFIX);

      let markerIdx = -1;
      let markerType: "dashboard" | "render" = "dashboard";

      if (dashIdx >= 0 && renderIdx >= 0) {
        if (dashIdx <= renderIdx) {
          markerIdx = dashIdx;
          markerType = "dashboard";
        } else {
          markerIdx = renderIdx;
          markerType = "render";
        }
      } else if (dashIdx >= 0) {
        markerIdx = dashIdx;
        markerType = "dashboard";
      } else if (renderIdx >= 0) {
        markerIdx = renderIdx;
        markerType = "render";
      }

      if (markerIdx < 0) {
        // No complete marker prefix found. Check for partial prefix at the end.
        if (!force) {
          const partialIdx = this.findPartialPrefix();
          if (partialIdx >= 0) {
            text += this.buffer.substring(0, partialIdx);
            this.buffer = this.buffer.substring(partialIdx);
            return { text, markers };
          }
        }
        text += this.buffer;
        this.buffer = "";
        return { text, markers };
      }

      // Emit text before the marker
      text += this.buffer.substring(0, markerIdx);
      this.buffer = this.buffer.substring(markerIdx);

      // Try to extract the complete marker
      const result =
        markerType === "dashboard"
          ? this.extractDashboardMarker()
          : this.extractRenderMarker();

      if (result === null) {
        // Incomplete marker — wait for more data, or flush as text
        if (force) {
          text += this.buffer;
          this.buffer = "";
        }
        return { text, markers };
      }

      if (result.marker) {
        markers.push(result.marker);
      } else {
        // Malformed marker, emit as text
        text += result.raw;
      }
      this.buffer = this.buffer.substring(result.raw.length);
    }

    return { text, markers };
  }

  /**
   * Extract [DASHBOARD:{...}] from the start of buffer.
   * Returns null if incomplete, { raw, marker? } if complete/malformed.
   */
  private extractDashboardMarker(): {
    raw: string;
    marker?: DashboardMarkerEvent;
  } | null {
    if (!this.buffer.startsWith(DASHBOARD_PREFIX)) return null;

    const jsonStart = DASHBOARD_PREFIX.length;
    if (jsonStart >= this.buffer.length) return null;

    if (this.buffer[jsonStart] !== "{") {
      // Not valid JSON after prefix
      return { raw: DASHBOARD_PREFIX };
    }

    const jsonEnd = this.findBalancedBraceEnd(jsonStart);
    if (jsonEnd === null) return null; // Incomplete JSON

    // Expect ] after the closing }
    if (jsonEnd >= this.buffer.length) return null;
    if (this.buffer[jsonEnd] !== "]") {
      // No closing bracket, malformed
      return { raw: this.buffer.substring(0, jsonEnd) };
    }

    const raw = this.buffer.substring(0, jsonEnd + 1);
    const jsonStr = this.buffer.substring(jsonStart, jsonEnd);

    try {
      const parsed = JSON.parse(jsonStr);
      return {
        raw,
        marker: {
          type: "dashboard",
          intent: parsed.intent || "custom",
          data: parsed.data,
        },
      };
    } catch {
      return { raw };
    }
  }

  /**
   * Extract [RENDER:component_name:{...}] from the start of buffer.
   * Returns null if incomplete, { raw, marker? } if complete/malformed.
   */
  private extractRenderMarker(): {
    raw: string;
    marker?: RenderMarkerEvent;
  } | null {
    if (!this.buffer.startsWith(RENDER_PREFIX)) return null;

    const afterPrefix = RENDER_PREFIX.length;

    // Find the component name (up to next :)
    const colonIdx = this.buffer.indexOf(":", afterPrefix);
    if (colonIdx < 0) return null; // Incomplete

    const component = this.buffer.substring(afterPrefix, colonIdx);
    const jsonStart = colonIdx + 1;

    if (jsonStart >= this.buffer.length) return null;

    if (this.buffer[jsonStart] === "{") {
      // JSON object — use balanced brace matching
      const jsonEnd = this.findBalancedBraceEnd(jsonStart);
      if (jsonEnd === null) return null;

      if (jsonEnd >= this.buffer.length || this.buffer[jsonEnd] !== "]") {
        return { raw: this.buffer.substring(0, jsonEnd >= this.buffer.length ? jsonEnd : jsonEnd + 1) };
      }

      const raw = this.buffer.substring(0, jsonEnd + 1);
      const propsStr = this.buffer.substring(jsonStart, jsonEnd);

      try {
        const props = JSON.parse(propsStr);
        return { raw, marker: { type: "render", component, props } };
      } catch {
        return { raw };
      }
    }

    // Non-object props — find closing ]
    const closeBracket = this.buffer.indexOf("]", jsonStart);
    if (closeBracket < 0) return null;

    const raw = this.buffer.substring(0, closeBracket + 1);
    const propsStr = this.buffer.substring(jsonStart, closeBracket);

    try {
      const props = JSON.parse(propsStr);
      return { raw, marker: { type: "render", component, props } };
    } catch {
      return { raw };
    }
  }

  /**
   * Find the position AFTER the balanced closing } starting from an opening {.
   * Returns the index after the closing }, or null if braces aren't balanced yet.
   */
  private findBalancedBraceEnd(start: number): number | null {
    let depth = 0;
    let inString = false;
    let escaped = false;

    for (let i = start; i < this.buffer.length; i++) {
      const ch = this.buffer[i];

      if (escaped) {
        escaped = false;
        continue;
      }

      if (ch === "\\") {
        escaped = true;
        continue;
      }

      if (ch === '"') {
        inString = !inString;
        continue;
      }

      if (inString) continue;

      if (ch === "{") depth++;
      if (ch === "}") {
        depth--;
        if (depth === 0) {
          return i + 1; // Position after the closing }
        }
      }
    }

    return null; // Not yet balanced
  }

  /**
   * Find where a partial marker prefix starts at the end of buffer.
   * Returns the index of the `[` or -1.
   */
  private findPartialPrefix(): number {
    const maxLen = Math.max(DASHBOARD_PREFIX.length, RENDER_PREFIX.length);

    for (
      let len = 1;
      len <= Math.min(maxLen, this.buffer.length);
      len++
    ) {
      const tail = this.buffer.substring(this.buffer.length - len);
      if (
        DASHBOARD_PREFIX.startsWith(tail) ||
        RENDER_PREFIX.startsWith(tail)
      ) {
        return this.buffer.length - len;
      }
    }

    return -1;
  }
}
