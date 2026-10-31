"use client";

import { Component, type ReactNode } from "react";

/**
 * A wall around one part of the tree.
 *
 * `app/src/app` carries no `error.tsx` and no `global-error.tsx`, so an
 * exception thrown while rendering ANY view takes the entire React tree down
 * and leaves the operator a blank window — no sidebar, no chat, no way back.
 * That is a disproportionate answer to one screen reading a field that a
 * separately-versioned engine stopped sending.
 *
 * Deliberately not a logger: React already writes the error and the component
 * stack to the console, and a second report from here would be a second place
 * a props value could be printed.
 */
interface ErrorBoundaryProps {
  children: ReactNode;
  /**
   * What to show instead. Rendered in place of the subtree that failed; a
   * function is handed the error, so the fallback can tell a chunk that did not
   * download from a component that threw.
   */
  fallback: ReactNode | ((error: unknown) => ReactNode);
}

interface ErrorBoundaryState {
  failed: boolean;
  error?: unknown;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(error: unknown): ErrorBoundaryState {
    return { failed: true, error };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    const { fallback } = this.props;
    return typeof fallback === "function" ? fallback(this.state.error) : fallback;
  }
}
