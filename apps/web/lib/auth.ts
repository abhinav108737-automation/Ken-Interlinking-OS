/**
 * Role and permission presentation for the reviewer console (ADR-0013 section 3).
 *
 * The console never decides authorisation: it HIDES controls the session's
 * permissions (from `GET /auth/me`) do not include, and the API enforces every
 * permission anyway. Labels follow the lib/status.ts convention: a `Record`
 * keyed by the generated enum (so a contract change fails type-checking), with
 * an explicit "Unknown (…)" fallback for a value this build does not know.
 */
import type { Permission, PrincipalView, Role } from "./api-client";

function hasOwn<T extends object>(record: T, key: string): key is Extract<keyof T, string> {
  return Object.prototype.hasOwnProperty.call(record, key);
}

function unknownLabel(value: string): string {
  return `Unknown (${value.trim() === "" ? "empty" : value})`;
}

const ROLE: Readonly<Record<Role, { readonly label: string; readonly description: string }>> = {
  viewer: { label: "Viewer", description: "Reads everything a reviewer reads; changes nothing." },
  reviewer: {
    label: "Reviewer",
    description: "Approves, rejects and edits recommendations, recording structured feedback.",
  },
  operator_admin: {
    label: "Operator admin",
    description: "Everything a reviewer may do, plus deploying approved links, the audit log and read-only settings.",
  },
};

const PERMISSION_LABEL: Readonly<Record<Permission, string>> = {
  read: "Read",
  review: "Review recommendations",
  read_audit: "Read the audit log",
  read_settings: "Read settings and policies",
  deploy: "Deploy, verify and roll back links",
};

export const ROLES = Object.keys(ROLE) as readonly Role[];
export const PERMISSIONS = Object.keys(PERMISSION_LABEL) as readonly Permission[];

export const describeRole = (value: string): string => (hasOwn(ROLE, value) ? ROLE[value].label : unknownLabel(value));
export const describeRoleDetail = (value: string): string =>
  hasOwn(ROLE, value) ? ROLE[value].description : "A role this console does not recognise.";
export const describePermission = (value: string): string =>
  hasOwn(PERMISSION_LABEL, value) ? PERMISSION_LABEL[value] : unknownLabel(value);

/** The session user's permissions, exactly as the API returned them; none when there is no user. */
export type PermissionSet = ReadonlySet<string>;

export function permissionsOf(user: Pick<PrincipalView, "permissions"> | null | undefined): PermissionSet {
  return new Set(user?.permissions ?? []);
}

export function can(permissions: PermissionSet, permission: Permission): boolean {
  return permissions.has(permission);
}
