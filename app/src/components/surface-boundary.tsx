"use client";

import { Component, Fragment, type ComponentType, type ReactNode } from "react";
import { ErrorNotice } from "@/components/error-notice";

/**
 * A wall around one surface of the shell: the chat panel, or one view.
 *
 * A render error inside it shows a notice in the surface's place; the sidebar,
 * the chat and every other view keep working. Retry re-mounts the subtree
 * (a fresh key, so its state starts over), and Report sends the message to the
 * dashboard error reporter. Views stay mounted while hidden, so a failed one
 * that is not on screen shows nothing until the operator opens it.
 *
 * Deliberately not a logger: React already writes the error and the component
 * stack to the console, and Report is the operator's choice.
 */
interface SurfaceBoundaryProps {
  /** What the operator calls this surface, e.g. "Chat" or "Tasks". */
  name: string;
  /** False while the surface is hidden. Defaults to true. */
  visible?: boolean;
  children: ReactNode;
}

interface SurfaceBoundaryState {
  error: Error | null;
  attempt: number;
}

const slug = (name: string) => name.toLowerCase().replace(/[^a-z0-9]+/g, "-");

export class SurfaceBoundary extends Component<SurfaceBoundaryProps, SurfaceBoundaryState> {
  state: SurfaceBoundaryState = { error: null, attempt: 0 };

  static getDerivedStateFromError(error: unknown): Partial<SurfaceBoundaryState> {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  private retry = () => {
    this.setState((s) => ({ error: null, attempt: s.attempt + 1 }));
  };

  render() {
    const { name, visible = true, children } = this.props;
    const { error, attempt } = this.state;
    if (!error) return <Fragment key={attempt}>{children}</Fragment>;
    if (!visible) return null;
    return (
      <div className="h-full overflow-y-auto p-4">
        <ErrorNotice
          key={attempt}
          testId={`surface-error-${slug(name)}`}
          title={`${name} failed to render`}
          report={{ source: `surface/${slug(name)}`, message: error.message }}
          onRetry={this.retry}
        >
          Something in it threw while drawing, usually an answer from the engine in a shape this
          version of the dashboard does not know. Nothing has been changed by the failure; the rest
          of the Helm keeps working.
        </ErrorNotice>
      </div>
    );
  }
}

/**
 * `Component` behind its own `SurfaceBoundary`, keeping its name and props.
 * The boundary takes its visibility from the view's own `visible` prop, so a
 * view that failed while hidden stays hidden.
 */
export function withSurfaceBoundary<P extends { visible?: boolean }>(
  name: string,
  Inner: ComponentType<P>,
): ComponentType<P> {
  function Guarded(props: P) {
    return (
      <SurfaceBoundary name={name} visible={props.visible ?? true}>
        <Inner {...props} />
      </SurfaceBoundary>
    );
  }
  Guarded.displayName = `withSurfaceBoundary(${name})`;
  return Guarded;
}
