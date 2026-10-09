import { loginUrl } from "../api/client";

export function LoginPage() {
  return (
    <main className="centered">
      <div className="panel narrow">
        <h1>SEWEB CRM</h1>
        <p>Sign in with your organisation account to continue.</p>
        <a className="button button-primary" href={loginUrl()}>
          Sign in
        </a>
      </div>
    </main>
  );
}
