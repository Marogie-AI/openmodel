import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * Without this, any throw during render unmounts the whole tree and the tab goes
 * blank with nothing to read — which is exactly how a one-character mistake in
 * the streaming code presented itself. A boundary turns that into a message.
 */
export default class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  override state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error): { error: Error } {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("console crashed", error, info.componentStack);
  }

  override render(): ReactNode {
    const { error } = this.state;
    if (error === null) return this.props.children;

    return (
      <section>
        <h2>The console hit a bug</h2>
        <p className="error">{error.message}</p>
        <p className="muted small">
          The details are in the browser console. Reloading is safe — your key is kept.
        </p>
        <button type="button" onClick={() => this.setState({ error: null })}>
          Try again
        </button>
      </section>
    );
  }
}
