import { Drawer } from "antd";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import Library from "./Library";
import PaperManagement from "../PaperManagement";

export default function LibraryManagement() {
  const [params, setParams] = useSearchParams();
  const [revision, setRevision] = useState(0);
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
