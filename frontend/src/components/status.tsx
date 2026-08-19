import { useI18n } from "../i18n/i18n-context";
import type { MessageKey } from "../i18n/messages";

export const JOB_STATUS_LABELS: Record<string, MessageKey> = {
  queued: "status.queued",
  starting: "status.starting",
  running: "status.running",
  stopping: "status.stopping",
  succeeded: "status.succeeded",
  failed: "status.failed",
  cancelled: "status.cancelled",
  interrupted: "status.interrupted",
};

export const ACTIVE_STATUSES = ["queued", "starting", "running", "stopping"];

/** Four states worth telling apart: in flight, done, broken, and everything waiting. */
function toneOf(status: string): string {
  if (status === "running" || status === "starting" || status === "stopping") {
    return "status-active";
  }
  if (status === "succeeded") {
    return "status-success";
  }
  if (status === "failed" || status === "interrupted") {
    return "status-danger";
  }
  return "status-neutral";
}

export function StatusLabel({ status }: { status: string }) {
  const { t } = useI18n();
  const key = JOB_STATUS_LABELS[status];
  return (
    <span className={`status ${toneOf(status)}`}>
      {key === undefined ? status : t(key)}
    </span>
  );
}
