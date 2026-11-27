import { useEffect, useState } from "react";
import { ConfigError } from "../../api";
import type { ConfigChange } from "../../types";
import { Button, Card } from "../../ui";
import { ChangeTable, Errors } from "./ConfigFeedback";

/**
 * Save flow: 저장 -> the server validates and returns the changes (dry run) -> the owner confirms -> saved, recorded in
 * the history and backed up. Invalid values come back as errors and nothing is saved.
 */
export function SaveBar({ dirty, run, onReset, onSaved }: {
  dirty: boolean;
  run: (dry: boolean) => Promise<{ changes: ConfigChange[] }>;
  onReset: () => void;
  onSaved: () => void;
}) {
  const [changes, setChanges] = useState<ConfigChange[] | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState("");
  useEffect(() => {
    setChanges(null);
    setErrors([]);
    if (dirty) setDone("");
  }, [dirty]);
  const go = async (dry: boolean) => {
    setBusy(true);
    setErrors([]);
    try {
      const r = await run(dry);
      if (dry) {
        if (r.changes.length) setChanges(r.changes);
        else setDone("바뀐 값이 없어요");
      } else {
        setChanges(null);
        setDone(`${r.changes.length}개 항목을 저장했어요 · 이력과 백업에 남았고, 다음 tick부터 적용돼요`);
        onSaved();
      }
    } catch (e) {
      setChanges(null);
      setErrors(e instanceof ConfigError ? e.errors : [String(e)]);
    } finally {
      setBusy(false);
    }
  };
  if (!dirty && !done && !errors.length) return null;
  return (
    <Card className="sticky bottom-3 z-10 mt-6 space-y-3 p-4">
      <Errors errors={errors} />
      {changes ? (
        <>
          <p className="text-sm font-semibold text-text-main">이 {changes.length}개 항목을 바꿀까요?</p>
          <ChangeTable changes={changes} />
          <div className="flex gap-2">
            <Button size="sm" disabled={busy} onClick={() => go(false)}>
              저장
            </Button>
            <Button size="sm" variant="secondary" disabled={busy} onClick={() => setChanges(null)}>
              취소
            </Button>
          </div>
        </>
      ) : dirty ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="mr-auto text-sm text-text-muted">저장하지 않은 변경이 있어요</span>
          <Button size="sm" variant="secondary" disabled={busy} onClick={onReset}>
            되돌리기
          </Button>
          <Button size="sm" loading={busy} onClick={() => go(true)}>
            변경 확인 후 저장
          </Button>
        </div>
      ) : (
        done && <p className="text-sm text-success-700">{done}</p>
      )}
    </Card>
  );
}
