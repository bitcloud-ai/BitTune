const SENSITIVE_KEY = /^(?:api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|token|password|secret|cookie)$/i;
const BEARER = /(bearer\s+)[^\s"']+/gi;
const SENSITIVE_ASSIGNMENT = /((?:api[_-]?key|authorization|token|password|secret|cookie)\s*[=:]\s*)(?!Bearer\b)("[^"]*"|'[^']*'|[^\s,;]+)/gi;
const SENSITIVE_FLAG = /(--?(?:api[_-]?key|authorization|token|password|secret|cookie))(?:\s+|=)([^\s"']+)/gi;

export function redact(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(redact);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([key, child]) => [
      key,
      SENSITIVE_KEY.test(key) ? "[REDACTED]" : redact(child),
    ]));
  }
  if (typeof value === "string") return redactText(value);
  return value;
}

export function redactText(value: string): string {
  return value.replace(BEARER, "$1[REDACTED]").replace(SENSITIVE_ASSIGNMENT, "$1[REDACTED]").replace(SENSITIVE_FLAG, "$1 [REDACTED]");
}
