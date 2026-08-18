import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";

import { server } from "./server";

const nativeWebSocket = globalThis.WebSocket;

/**
 * Node's experimental web storage shadows jsdom's here and leaves an empty object behind,
 * so tests install a real Storage rather than depending on the runtime's version.
 */
function createStorage(): Storage {
  const entries = new Map<string, string>();
  return {
    get length() {
      return entries.size;
    },
    clear: () => entries.clear(),
    getItem: (key: string) => entries.get(key) ?? null,
    key: (index: number) => [...entries.keys()][index] ?? null,
    removeItem: (key: string) => void entries.delete(key),
    setItem: (key: string, value: string) => void entries.set(key, String(value)),
  } as Storage;
}

const storage = createStorage();
for (const target of [globalThis, window]) {
  Object.defineProperty(target, "localStorage", {
    value: storage,
    writable: true,
    configurable: true,
  });
}

/** WebSocket is a read-only global here, so it can only be swapped by redefining it. */
function setWebSocket(implementation: typeof WebSocket): void {
  Object.defineProperty(globalThis, "WebSocket", {
    value: implementation,
    writable: true,
    configurable: true,
  });
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));

afterEach(() => {
  cleanup();
  server.resetHandlers();
  // A test that installed a fake socket must not leak it into the next one.
  setWebSocket(nativeWebSocket);
  storage.clear();
});

afterAll(() => server.close());
