import { useState } from "react";
import type { FormEvent } from "react";

import { useAuth } from "../auth/auth-context";
import { useI18n } from "../i18n/i18n-context";

type Panel = "login" | "register";

export function AuthPage() {
  const { t } = useI18n();
  const { login, register } = useAuth();
  const [panel, setPanel] = useState<Panel>("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await (panel === "login" ? login(username, password) : register(username, password));
    } catch (failure) {
      // Show what the server actually said; a generic message hides the fix.
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={handleSubmit}>
        <h1 className="auth-title">{t("app.name")}</h1>
        {/* A tablist, not two buttons: in the sign-in panel the submit button shares the
            "login" label, and two same-named buttons are ambiguous to assistive technology. */}
        <div className="segmented" role="tablist" aria-label={t("app.name")}>
          <button
            type="button"
            role="tab"
            className="segmented-option"
            aria-selected={panel === "login"}
            onClick={() => {
              setPanel("login");
              setError(null);
            }}
          >
            {t("auth.login")}
          </button>
          <button
            type="button"
            role="tab"
            className="segmented-option"
            aria-selected={panel === "register"}
            onClick={() => {
              setPanel("register");
              setError(null);
            }}
          >
            {t("auth.register")}
          </button>
        </div>

        <label className="field" htmlFor="auth-username">
          {t("auth.username")}
        </label>
        <input
          id="auth-username"
          className="input"
          value={username}
          autoComplete="username"
          onChange={(event) => setUsername(event.target.value)}
        />

        <label className="field" htmlFor="auth-password">
          {t("auth.password")}
        </label>
        <input
          id="auth-password"
          className="input"
          type="password"
          value={password}
          autoComplete={panel === "login" ? "current-password" : "new-password"}
          onChange={(event) => setPassword(event.target.value)}
        />
        {panel === "register" && <p className="field-hint">{t("auth.passwordHint")}</p>}

        {error !== null && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}

        <button type="submit" className="button-primary" disabled={submitting}>
          {panel === "login" ? t("auth.login") : t("auth.createAccount")}
        </button>
      </form>
    </div>
  );
}
