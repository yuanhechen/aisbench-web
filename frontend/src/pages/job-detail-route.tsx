import { useParams } from "react-router-dom";

import { JobDetailPage } from "./job-detail-page";

export function JobDetailRoute() {
  const { jobId } = useParams();
  return jobId === undefined ? null : <JobDetailPage jobId={jobId} />;
}
