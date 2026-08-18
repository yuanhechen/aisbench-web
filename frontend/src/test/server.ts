import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";

// Signed out by default: a test that needs a session says so explicitly.
export const server = setupServer(
  http.get("/api/me", () => HttpResponse.json({ detail: "not authenticated" }, { status: 401 })),
);
