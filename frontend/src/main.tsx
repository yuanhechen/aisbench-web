import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app";
// Self-hosted faces: this tool runs on intranet boxes with no route to a font CDN, so
// the dashboard pairing ships in the bundle. Sans reads, Code counts.
import "@fontsource/fira-sans/400.css";
import "@fontsource/fira-sans/500.css";
import "@fontsource/fira-sans/600.css";
import "@fontsource/fira-code/400.css";
import "@fontsource/fira-code/500.css";
import "./styles.css";

const container = document.getElementById("root");
if (container === null) {
  throw new Error("The #root element is missing from index.html");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
