import { describe, expect, it } from "vitest";

import { MESSAGES } from "./messages";

describe("message dictionaries", () => {
  it("define exactly the same keys in every locale", () => {
    const locales = Object.keys(MESSAGES) as (keyof typeof MESSAGES)[];
    const reference = Object.keys(MESSAGES.zh).sort();

    for (const locale of locales) {
      expect(Object.keys(MESSAGES[locale]).sort()).toEqual(reference);
    }
  });

  it("leave no message empty", () => {
    for (const dictionary of Object.values(MESSAGES)) {
      for (const [key, value] of Object.entries(dictionary)) {
        expect(value, key).not.toBe("");
      }
    }
  });
});
