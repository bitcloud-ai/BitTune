import { randomUUID, createHash } from "node:crypto";
import { BittuneError } from "./errors.ts";

const SAFE_ID = /^[a-z][a-z0-9-]{0,62}$/;
const VERSION = /^v[1-9][0-9]*$/;
const SHA256 = /^sha256:[a-f0-9]{64}$/;

export function newId(prefix: string): string {
  return `${prefix}-${randomUUID()}`;
}

export function requireId(value: string, field: string): string {
  if (!SAFE_ID.test(value)) throw new BittuneError("invalid_input", `${field} 必须是小写字母开头的稳定 ID。`);
  return value;
}

export function requireVersion(value: string, field = "version"): string {
  if (!VERSION.test(value)) throw new BittuneError("invalid_input", `${field} 必须采用 vN 格式。`);
  return value;
}

export function requireSha256(value: string, field = "sha256"): string {
  if (!SHA256.test(value)) throw new BittuneError("invalid_input", `${field} 必须是 sha256:<64 hex>。`);
  return value;
}

export function sha256(value: string | Uint8Array): string {
  return `sha256:${createHash("sha256").update(value).digest("hex")}`;
}

export function canonicalJson(value: unknown): string {
  const normalize = (item: unknown): unknown => {
    if (Array.isArray(item)) return item.map(normalize);
    if (item && typeof item === "object") {
      return Object.fromEntries(Object.entries(item as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, child]) => [key, normalize(child)]));
    }
    return item;
  };
  return JSON.stringify(normalize(value));
}
