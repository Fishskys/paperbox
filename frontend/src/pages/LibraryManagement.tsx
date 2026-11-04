import { App, Drawer } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";
import { useBeforeUnload, useBlocker, useSearchParams } from "react-router-dom";
import Library from "./Library";
import PaperManagement from "../PaperManagement";

export default function LibraryManagement() {
  const [params, setParams] = useSearchParams();
  const [revision, setRevision] = useState(0);
  const dirty = useRef(false);
  const { modal } = App.useApp();
  const blocker = useBlocker(() => dirty.current);
  useBeforeUnload(useCallback((event: BeforeUnloadEvent) => {
    if (dirty.current) { event.preventDefault(); event.returnValue = ""; }
  }, []));
  useEffect(() => {
    if (blocker.state !== "blocked") return;
    const confirmation = modal.confirm({
      title: "有未保存的元数据",
      content: "离开将丢弃当前修改。",
      okText: "丢弃修改",
      cancelText: "继续编辑",
      onOk: () => { dirty.current = false; blocker.proceed(); },
      onCancel: () => blocker.reset(),
    });
    return () => confirmation.destroy();
  }, [blocker, modal]);
  const paperId = params.get("paper");
  function close() {
    const next = new URLSearchParams(params);
    next.delete("paper");
    setParams(next);
  }
  return (
    <>
      <Library
        title="论文库管理"
        revision={revision}
        onManage={(paper) => {
          const next = new URLSearchParams(params);
          next.set("paper", paper.paper_id);
          setParams(next);
        }}
      />
      <Drawer
        title="管理论文"
        size="large"
        styles={{ wrapper: { maxWidth: "100vw" } }}
        open={!!paperId}
        onClose={close}
        destroyOnHidden
      >
        {paperId && (
          <PaperManagement
            key={paperId}
            paperId={paperId}
            onDirtyChange={(value) => { dirty.current = value; }}
            onChanged={() => setRevision((value) => value + 1)}
            onDeleted={() => {
              setRevision((value) => value + 1);
              close();
            }}
          />
        )}
      </Drawer>
    </>
  );
}
