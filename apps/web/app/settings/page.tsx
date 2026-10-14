import { redirect } from "next/navigation";

import { SETTINGS_POLICIES_PATH } from "@/lib/audit";

export default function SettingsIndex(): never {
  redirect(SETTINGS_POLICIES_PATH);
}
