import { useLayoutEffect, useMemo } from "react";
import { useI18n } from "@/i18n";
import { usePageHeader } from "@/contexts/usePageHeader";

/** Cloak Edition — embeds the standalone /cloak panel (proxy pool, captcha keys, token, profiles). */
export default function CloakPage() {
  const { t } = useI18n();
  const { setTitle } = usePageHeader();

  const src = useMemo(() => {
    const base =
      (typeof window !== "undefined" &&
        (window as Window & { __HERMES_BASE_PATH__?: string })
          .__HERMES_BASE_PATH__) ||
      "";
    return `${base}/cloak`;
  }, []);

  useLayoutEffect(() => {
    setTitle(t.app.nav.cloak ?? "Cloak");
  }, [setTitle, t.app.nav.cloak]);

  return (
    <iframe
      src={src}
      title={t.app.nav.cloak ?? "Cloak Manager"}
      className="min-h-0 w-full flex-1 rounded-lg border border-border/40 bg-background"
      style={{ minHeight: "calc(100vh - 10rem)", height: "calc(100vh - 10rem)" }}
    />
  );
}
