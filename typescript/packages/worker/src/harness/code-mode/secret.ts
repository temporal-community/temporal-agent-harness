// The secret Code Mode workers sign resume state with: Monty snapshots and `run` continuations.
// Worker side only.

/** The justfiles' local-dev RUN_CONTINUATION_SECRET. It is public, so anyone can forge state signed with it. */
export const DEV_CONTINUATION_SECRET = "local-dev-only-code-mode-continuation-secret";

/**
 * `secret`, checked: at least 32 bytes, and never the published local-dev value when NODE_ENV is
 * production (outside it, a warning). Every worker that resumes a script needs the same one.
 */
export function continuationSecret(secret: string | undefined, what: string): string {
  if (!secret || new TextEncoder().encode(secret).length < 32) {
    throw new Error(
      `${what} needs a continuation secret of at least 32 bytes: set RUN_CONTINUATION_SECRET on every worker.`,
    );
  }
  if (secret === DEV_CONTINUATION_SECRET) {
    const fix =
      "generate your own (`openssl rand -hex 32`) and set it as RUN_CONTINUATION_SECRET on every worker";
    if (process.env.NODE_ENV === "production")
      throw new Error(
        `RUN_CONTINUATION_SECRET is the published local-dev value, which anyone can sign with; ${fix}.`,
      );
    console.warn(
      `WARNING: ${what} signs paused scripts with the published local-dev RUN_CONTINUATION_SECRET; anyone can forge them. Outside local dev, ${fix}.`,
    );
  }
  return secret;
}
