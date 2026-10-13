import type { PlatformBridge } from "./types";

export const browserPlatform: PlatformBridge = {
  async getPlatform() {
    return "web";
  },

  async showNotification(title, body) {
    if (typeof window === "undefined") {
      return;
    }

    if (!("Notification" in window)) {
      return;
    }

    // Never request permission here: prompting is a user-initiated UI decision.
    if (Notification.permission === "granted") {
      try {
        new Notification(title, { body });
      } catch {
        // Some browsers (e.g. Android Chrome) only allow service-worker
        // notifications and throw from the constructor. Best-effort: ignore.
      }
    }
  },
};
