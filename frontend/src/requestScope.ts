/** Guards UI writes even when a server or transport ignores cancellation. */
export class RequestScope {
  private sequence = 0;
  private active: { key: string; token: number; controller: AbortController } | null = null;
  begin(key: string) {
    this.cancel();
    const current = { key, token: ++this.sequence, controller: new AbortController() };
    this.active = current;
    return { signal: current.controller.signal, isCurrent: () => this.active === current && !current.controller.signal.aborted };
  }
  cancel() { this.active?.controller.abort(); this.active = null; }
}
