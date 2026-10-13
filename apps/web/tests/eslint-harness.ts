import { fileURLToPath } from "node:url";

import { ESLint } from "eslint";

/** apps/web — where eslint.config.mjs lives. */
export const WEB_ROOT = fileURLToPath(new URL("..", import.meta.url));

let instance: ESLint | undefined;

function eslint(): ESLint {
  instance ??= new ESLint({ cwd: WEB_ROOT });
  return instance;
}

export interface LintFinding {
  readonly ruleId: string | null;
  readonly message: string;
  readonly severity: number;
}

/** Lint `code` as if it lived at `filePath` (relative to apps/web). The file need not exist. */
export async function lintAs(filePath: string, code: string): Promise<LintFinding[]> {
  const [result] = await eslint().lintText(code, { filePath });
  if (result === undefined) throw new Error(`no lint result for ${filePath}`);
  return result.messages.map(({ ruleId, message, severity }) => ({ ruleId, message, severity }));
}

export const GUARD_RULES = new Set([
  "no-restricted-imports",
  "no-restricted-syntax",
  "no-restricted-globals",
]);

/** Errors raised by the architecture guards only (ignores unrelated rules such as unused vars). */
export async function guardErrors(filePath: string, code: string): Promise<LintFinding[]> {
  return (await lintAs(filePath, code)).filter(
    (finding) => finding.severity === 2 && finding.ruleId !== null && GUARD_RULES.has(finding.ruleId),
  );
}
