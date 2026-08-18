import { useI18n } from "../i18n/i18n-context";
import { PageHeader } from "../components/page-header";

export function ComparisonPage() {
  const { t } = useI18n();
  return <PageHeader title={t("nav.comparison")} subtitle={t("comparison.subtitle")} />;
}
