import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";

import { server } from "./server";

const nativeWebSocket = globalThis.WebSocket;

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
});

afterAll(() => server.close());
