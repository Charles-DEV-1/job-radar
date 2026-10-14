import { Brand } from "@/components/brand";
import { LoginForm } from "@/components/login-form";
export const dynamic = "force-dynamic";
export default function LoginPage() {
  return (
    <main className="login-shell">
      <section className="login-card">
        <Brand />
        <h1>Your next opportunity.</h1>
        <p className="notice">Sign in to your private job radar.</p>
        <LoginForm />
        <p className="notice" style={{ marginTop: 24 }}>
          Access is limited to the configured account.
        </p>
      </section>
    </main>
  );
}
