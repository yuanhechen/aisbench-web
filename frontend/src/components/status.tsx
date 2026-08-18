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

/** Blue marks work in flight; a finished job is neutral and a failure is the one danger colour. */
function toneOf(status: string): string {
  if (status === "running" || status === "starting") {
    return "status-active";
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
