import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";

// Signed out by default, and every resource empty: a test that needs data says so explicitly.
export const server = setupServer(
  http.get("/api/me", () => HttpResponse.json({ detail: "not authenticated" }, { status: 401 })),
  http.get("/api/models", () => HttpResponse.json([])),
  http.get("/api/datasets", () => HttpResponse.json([])),
  http.get("/api/jobs", () => HttpResponse.json([])),
);
