import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Check, Send } from "lucide-react";
import { toast } from "sonner";
import { request } from "@/lib/api";
import { ConfirmDialog } from "@/components/common/ConfirmDialog";

type PrecheckResult = "PASS" | "FAIL" | "N/A";

interface HandoffState {
  ir: boolean;
  strategy_id: string | null;
  name: string | null;
  window: { start_day: string; end_day: string } | null;
  robustness: { grade: string; score: number; caps: string[]; schema: string; sha256: string } | null;
  precheck: { id: string; label: string; result: PrecheckResult; observed: unknown; note: string | null }[];
  protocol: { version: string; sha256: string };
  target: { container_path: string; host_path: string } | null;
  exported: { path: string; at: string; strategy_id: string; files: { name: string; sha256: string }[] } | null;
  credential: boolean;
}

interface ExportResponse {
  strategy_id: string;
  path: string;
  files: { name: string; sha256: string }[];
  at: string;
  grade: string;
}

const RESULT_CLASS: Record<PrecheckResult, string> = {
  PASS: "text-success",
  FAIL: "text-danger",
  "N/A": "text-muted-foreground",
};

export function AlphaKeelHandoff({ runId }: { runId: string | undefined }) {
  const { t } = useTranslation();
  const [state, setState] = useState<HandoffState | null>(null);
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [reloads, setReloads] = useState(0);

  useEffect(() => {
    setState(null);
    if (!runId) return;
    let cancelled = false;
    (async () => {
      try {
        const data = await request<HandoffState>(`/runs/${encodeURIComponent(runId)}/alphakeel`);
        if (!cancelled) setState(data);
      } catch (err) {
        console.error("AlphaKeel hand-off state unavailable", err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runId, reloads]);

  const doExport = useCallback(async () => {
    if (!runId || pending) return;
    setPending(true);
    try {
      // the hash of the robustness the user is looking at: the server refuses when it changed since (export.changed)
      const res = await request<ExportResponse>(`/runs/${encodeURIComponent(runId)}/alphakeel/export`, {
        method: "POST",
        body: JSON.stringify({ robustness_sha256: state?.robustness?.sha256 ?? null }),
      });
      toast.success(t("runDetail.alphakeel.exportedTo", { path: res.path }));
      setState((prev) =>
        prev ? { ...prev, exported: { path: res.path, at: res.at, strategy_id: res.strategy_id, files: res.files } } : prev,
      );
      setOpen(false);
    } catch (err) {
      const e = err as { message?: string; code?: string };
      if (e.code === "export.changed") {
        toast.error(t("runDetail.alphakeel.exportChanged"));
        setOpen(false);
        setReloads((n) => n + 1);
      } else if (e.code === "export.window_mismatch") {
        toast.error(t("runDetail.alphakeel.exportWindowMismatch"));
      } else {
        toast.error(e.message || e.code || t("runDetail.alphakeel.exportFailed"));
      }
    } finally {
      setPending(false);
    }
  }, [runId, pending, state, t]);

  if (!state || state.ir !== true) return null;

  const missingRobustness = state.robustness === null;
  const missingCredential = state.credential === false;
  const disabled = missingRobustness || missingCredential;
  const exported = state.exported;
  const title = missingRobustness
    ? t("runDetail.alphakeel.needRobustness")
    : missingCredential
      ? t("runDetail.alphakeel.needCredential")
      : exported
        ? exported.path
        : undefined;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        disabled={disabled}
        title={title}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs text-muted-foreground hover:bg-muted/60 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {exported && !disabled ? <Check className="h-3.5 w-3.5" /> : <Send className="h-3.5 w-3.5" />}{" "}
        {exported && !disabled ? t("runDetail.alphakeel.exported") : t("runDetail.alphakeel.button")}
      </button>
      <ConfirmDialog
        open={open}
        tone="primary"
        title={t("runDetail.alphakeel.confirmTitle")}
        confirmLabel={t("runDetail.alphakeel.confirm")}
        cancelLabel={t("runDetail.alphakeel.cancel")}
        confirmDisabled={pending}
        onConfirm={doExport}
        onCancel={() => setOpen(false)}
      >
        <div className="space-y-2 text-xs text-foreground">
          <div className="font-mono break-all">{state.strategy_id}</div>
          {state.name && <div>{state.name}</div>}
          {state.window && (
            <div className="text-muted-foreground">
              {t("runDetail.alphakeel.window")}: {state.window.start_day} → {state.window.end_day}
            </div>
          )}
          {state.robustness && (
            <div>
              {t("runDetail.alphakeel.grade")}: <span className="font-medium">{state.robustness.grade}</span>
              {state.robustness.caps.length > 0 && (
                <span className="text-muted-foreground"> ({state.robustness.caps.join(", ")})</span>
              )}
            </div>
          )}
          {state.precheck.length > 0 && (
            <div>
              <div className="mb-1 text-muted-foreground">{t("runDetail.alphakeel.precheckTitle")}</div>
              <table className="w-full text-left">
                <tbody>
                  {state.precheck.map((p) => (
                    <tr key={p.id} className="border-t">
                      <td className="py-0.5 pr-2 font-mono">{p.id}</td>
                      <td className="py-0.5 pr-2">{p.label}</td>
                      <td className={`py-0.5 text-right font-medium ${RESULT_CLASS[p.result] ?? "text-muted-foreground"}`}>
                        {p.result}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {state.target && (
            <div>
              <span className="text-muted-foreground">{t("runDetail.alphakeel.target")}: </span>
              <span className="font-mono break-all">{state.target.host_path}</span>
            </div>
          )}
          {exported && <p className="text-muted-foreground">{t("runDetail.alphakeel.reexportNote")}</p>}
          <p className="text-muted-foreground">{t("runDetail.alphakeel.notVerdict")}</p>
        </div>
      </ConfirmDialog>
    </>
  );
}
