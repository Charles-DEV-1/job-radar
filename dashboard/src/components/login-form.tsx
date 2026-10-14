"use client";
import { useActionState } from "react";
import { signIn } from "@/app/login/actions";
export function LoginForm() {
  const [state, action, pending] = useActionState(signIn, { error: null });
  return (
    <form action={action}>
      <label className="field">
        Email
        <input
          name="email"
          type="email"
          required
          autoComplete="email"
          autoCapitalize="none"
          spellCheck={false}
        />
      </label>
      <label className="field">
        Password
        <input
          name="password"
          type="password"
          required
          autoComplete="current-password"
          maxLength={256}
        />
      </label>
      {state.error && (
        <p role="alert" className="error">
          {state.error}
        </p>
      )}
      <button className="button primary" disabled={pending}>
        {pending ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}
