import { describe, expect, it } from "vitest";

import { parseAnsi } from "./log-view";

const ESC = "";

describe("run log", () => {
  it("turns the colours AISBench emits into styling, not into text", () => {
    // Printed literally this reads "[32mINFO [0m", which is noise where the colour was
    // information.
    const spans = parseAnsi(`[ais_bench] [ ${ESC}[32mINFO ${ESC}[0m] done`);

    expect(spans.map((span) => span.text).join("")).toBe("[ais_bench] [ INFO ] done");
    expect(spans.find((span) => span.text === "INFO ")?.className).toBe("ansi-green");
  });

  it("leaves a bracket that is not an escape sequence alone", () => {
    // "[ais_bench]" begins with a bracket and ends with a letter; matching on the bracket
    // rather than the ESC would swallow it.
    const spans = parseAnsi("[ais_bench] [32 files]");

    expect(spans.map((span) => span.text).join("")).toBe("[ais_bench] [32 files]");
    expect(spans.every((span) => span.className === "")).toBe(true);
  });

  it("keeps only what a terminal would still be showing after a progress bar", () => {
    // tqdm redraws one line hundreds of times with carriage returns; every redraw but the
    // last is already gone from the screen it was written to.
    const spans = parseAnsi("setup\n 10%|\r 50%|\r100%|done\nnext\n");

    expect(spans.map((span) => span.text).join("")).toBe("setup\n100%|done\nnext\n");
  });

  it("drops cursor moves without dropping the text around them", () => {
    const spans = parseAnsi(`a${ESC}[2Kb${ESC}[1;1Hc`);

    expect(spans.map((span) => span.text).join("")).toBe("abc");
  });

  it("ends a colour where the reset says it ends", () => {
    const spans = parseAnsi(`plain${ESC}[31mred${ESC}[0mplain again`);

    expect(spans).toEqual([
      { text: "plain", className: "" },
      { text: "red", className: "ansi-red" },
      { text: "plain again", className: "" },
    ]);
  });
});
