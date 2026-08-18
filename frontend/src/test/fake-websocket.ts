type Listener = (event: unknown) => void;

export interface FakeSocket {
  url: string;
  readyState: number;
  open(): void;
  emitJson(value: unknown): void;
  close(): void;
  send(data: string): void;
  addEventListener(type: string, listener: Listener): void;
  removeEventListener(type: string, listener: Listener): void;
  onopen: Listener | null;
  onmessage: Listener | null;
  onclose: Listener | null;
  onerror: Listener | null;
  sent: string[];
}

export interface FakeWebSocketHandle {
  sockets: FakeSocket[];
  restore(): void;
}

/** Replace the global WebSocket so tests drive job events without a server. */
export function installFakeWebSocket(): FakeWebSocketHandle {
  const sockets: FakeSocket[] = [];
  const native = globalThis.WebSocket;

  class TestSocket implements FakeSocket {
    url: string;
    readyState = 0;
    sent: string[] = [];
    onopen: Listener | null = null;
    onmessage: Listener | null = null;
    onclose: Listener | null = null;
    onerror: Listener | null = null;
    private listeners = new Map<string, Set<Listener>>();

    constructor(url: string | URL) {
      this.url = String(url);
      sockets.push(this);
    }

    addEventListener(type: string, listener: Listener): void {
      const existing = this.listeners.get(type) ?? new Set<Listener>();
      existing.add(listener);
      this.listeners.set(type, existing);
    }

    removeEventListener(type: string, listener: Listener): void {
      this.listeners.get(type)?.delete(listener);
    }

    private dispatch(type: string, event: unknown): void {
      const handler = (this as unknown as Record<string, Listener | null>)[`on${type}`];
      handler?.call(this, event);
      this.listeners.get(type)?.forEach((listener) => listener(event));
    }

    open(): void {
      this.readyState = 1;
      this.dispatch("open", { type: "open" });
    }

    emitJson(value: unknown): void {
      this.dispatch("message", { type: "message", data: JSON.stringify(value) });
    }

    close(): void {
      this.readyState = 3;
      this.dispatch("close", { type: "close" });
    }

    send(data: string): void {
      this.sent.push(data);
    }
  }

  // WebSocket is a read-only global in jsdom, so swapping it requires redefining it.
  function setWebSocket(implementation: typeof WebSocket): void {
    Object.defineProperty(globalThis, "WebSocket", {
      value: implementation,
      writable: true,
      configurable: true,
    });
  }

  setWebSocket(TestSocket as unknown as typeof WebSocket);
  return {
    sockets,
    restore() {
      setWebSocket(native);
    },
  };
}
