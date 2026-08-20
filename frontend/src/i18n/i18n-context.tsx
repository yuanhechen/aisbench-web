import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { MESSAGES } from "./messages";
import type { Locale, MessageKey } from "./messages";

const STORAGE_KEY = "aisbench-web.locale";
const DEFAULT_LOCALE: Locale = "zh";

interface I18nValue {
  locale: Locale;
  /** Translate a key, filling `{name}` placeholders when the string carries them. */
  t: (key: MessageKey, params?: Record<string, string>) => string;
  toggleLocale: () => void;
}

const I18nContext = createContext<I18nValue | null>(null);

function storedLocale(): Locale {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    return saved === "en" || saved === "zh" ? saved : DEFAULT_LOCALE;
  } catch {
    // Private-mode browsers can refuse storage; the default locale still works.
    return DEFAULT_LOCALE;
  }
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocale] = useState<Locale>(storedLocale);

  const toggleLocale = useCallback(() => {
    setLocale((current) => {
      const next: Locale = current === "zh" ? "en" : "zh";
      try {
        localStorage.setItem(STORAGE_KEY, next);
      } catch {
        // Losing the preference is acceptable; losing the click is not.
      }
      return next;
    });
  }, []);

  const value = useMemo<I18nValue>(
    () => ({
      locale,
      toggleLocale,
      t: (key: MessageKey, params?: Record<string, string>) => {
        const template = MESSAGES[locale][key];
        if (params === undefined) {
          return template;
        }
        return template.replace(/\{(\w+)\}/g, (match, name: string) =>
          name in params ? params[name] : match,
        );
      },
    }),
    [locale, toggleLocale],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const value = useContext(I18nContext);
  if (value === null) {
    throw new Error("useI18n must be used inside an I18nProvider");
  }
  return value;
}
