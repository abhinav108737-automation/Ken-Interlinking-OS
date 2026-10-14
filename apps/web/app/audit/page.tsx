import { redirect } from "next/navigation";

import { AUDIT_REVIEWS_PATH } from "@/lib/audit";

export default function AuditIndex(): never {
  redirect(AUDIT_REVIEWS_PATH);
}
