"use client";

import { Component, type ErrorInfo, type ReactNode } from "react";
import { getComponent } from "@/lib/component-registry";
import { Skeleton } from "@/components/ui/skeleton";

interface ComponentRendererProps {
  toolName: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  props: Record<string, any>;
}

interface ErrorBoundaryProps {
  children: ReactNode;
  fallback: ReactNode;
}

interface ErrorBoundaryState {
  hasError: boolean;
}

class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { hasError: true };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error("Component render error:", error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      return this.props.fallback;
    }
    return this.props.children;
  }
}

export function ComponentRenderer({ toolName, props }: ComponentRendererProps) {
  const registration = getComponent(toolName);

  if (!registration) {
    return (
      <div
        className="p-4 text-sm text-muted-foreground"
        data-testid="unknown-component"
      >
        Unknown component: {toolName}
      </div>
    );
  }

  const Comp = registration.component;

  return (
    <ErrorBoundary
      fallback={
        <div className="p-4 text-sm text-destructive" data-testid="component-error">
          Error rendering {toolName}
        </div>
      }
    >
      <div data-testid="rendered-component">
        <Comp {...props} />
      </div>
    </ErrorBoundary>
  );
}

export function ComponentLoading() {
  return (
    <div className="p-4 space-y-3" data-testid="component-loading">
      <Skeleton className="h-8 w-48" />
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-4 w-3/4" />
      <Skeleton className="h-32 w-full" />
    </div>
  );
}
