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
  /** What to show instead. Rendered in place of the subtree that failed. */
  fallback: ReactNode;
}

interface ErrorBoundaryState {
  failed: boolean;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { failed: true };
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}
