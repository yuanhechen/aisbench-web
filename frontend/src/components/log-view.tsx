import { forwardRef, useMemo } from "react";

/**
 * A styled run of log text.
 *
 * AISBench colours its own output, so the log arrives with SGR escape sequences in it.
 * Printing them literally puts "[32mINFO [0m" on the page, which is noise where the colour
 * was information.
 */
interface Span {
  text: string;
  className: string;
}

// The ESC is what makes it an escape sequence: matching a bare "[" would eat
// "[ais_bench]". Only the final `m` (colour) means anything here; cursor moves and erases
// come from progress bars and are dropped.
const CSI = /\u001b\[([0-9;?]*)([A-Za-z])/g;
// Carriage returns rewrite the current line; a progress bar sends hundreds of them.
const OVERWRITTEN = /^.*\r(?!\n)/gm;

const SGR_CLASS: Record<number, string> = {
  1: "ansi-bold",
  30: "ansi-black",
  31: "ansi-red",
  32: "ansi-green",
  33: "ansi-yellow",
  34: "ansi-blue",
  35: "ansi-magenta",
  36: "ansi-cyan",
  37: "ansi-white",
  90: "ansi-dim",
  91: "ansi-red",
  92: "ansi-green",
  93: "ansi-yellow",
  94: "ansi-blue",
  95: "ansi-magenta",
  96: "ansi-cyan",
};

/** Split log text into spans, one per stretch that shares a style. */
export function parseAnsi(raw: string): Span[] {
  // Keep only what a terminal would still be showing after the carriage returns.
  const text = raw.replace(OVERWRITTEN, "");
  const spans: Span[] = [];
  let colour = "";
  let bold = "";
  let cursor = 0;

  const push = (chunk: string) => {
    if (chunk === "") {
      return;
    }
    const className = [colour, bold].filter((part) => part !== "").join(" ");
    const last = spans[spans.length - 1];
    if (last !== undefined && last.className === className) {
      last.text += chunk;
    } else {
      spans.push({ text: chunk, className });
    }
  };

  CSI.lastIndex = 0;
  for (let match = CSI.exec(text); match !== null; match = CSI.exec(text)) {
    push(text.slice(cursor, match.index));
    cursor = match.index + match[0].length;
    if (match[2] !== "m") {
      continue;
    }
    for (const part of match[1].split(";")) {
      const code = Number(part === "" ? "0" : part);
      if (code === 0) {
        colour = "";
        bold = "";
      } else if (code === 1) {
        bold = SGR_CLASS[1];
      } else if (code === 22) {
        bold = "";
      } else if (code === 39) {
        colour = "";
      } else if (SGR_CLASS[code] !== undefined) {
        colour = SGR_CLASS[code];
      }
    }
  }
  push(text.slice(cursor));
  return spans;
}

export const LogView = forwardRef<HTMLPreElement, { text: string; empty: string }>(
  function LogView({ text, empty }, ref) {
    const spans = useMemo(() => parseAnsi(text), [text]);
    return (
      <pre className="log-view" ref={ref}>
        {text === "" ? (
          <span className="log-empty">{empty}</span>
        ) : (
          spans.map((span, index) => (
            // Spans are positional by nature: the index is the identity.
            <span key={index} className={span.className}>
              {span.text}
            </span>
          ))
        )}
      </pre>
    );
  },
);
