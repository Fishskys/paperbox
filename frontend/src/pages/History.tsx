import { Button, Popconfirm, Tooltip } from "antd";
import { ArrowUpRight, History as HistoryIcon, Trash2 } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Blank, formatTime, PageHeader } from "../components";
import { readHistory, writeHistory } from "../history";
import { readerPath } from "../search-model";

export default function History({ kind }: { kind: "searches" | "readings" }) {
  const [revision, setRevision] = useState(0);
  const items =
    kind === "searches" ? readHistory("searches") : readHistory("readings");
  function remove(id?: string) {
    if (kind === "searches")
      writeHistory(
        "searches",
        id ? readHistory("searches").filter((item) => item.id !== id) : [],
      );
    else
      writeHistory(
        "readings",
        id ? readHistory("readings").filter((item) => item.id !== id) : [],
      );
    setRevision(revision + 1);
  }
  return (
    <>
      <PageHeader
        title={kind === "searches" ? "检索记录" : "最近阅读"}
        description="本机记录"
        actions={
          <Popconfirm title="清空本机记录？" onConfirm={() => remove()}>
            <Button disabled={!items.length} icon={<Trash2 size={15} />}>
              清空记录
            </Button>
          </Popconfirm>
        }
      />
      {!items.length && (
        <Blank text={kind === "searches" ? "暂无检索记录" : "暂无阅读记录"} />
      )}
      {items.map((item) => (
        <article className="history-row" key={item.id}>
          <span className="history-icon">
            <HistoryIcon size={18} />
          </span>
          <div className="history-content">
            <Link
              state={{
                from:
                  kind === "searches" ? "/search/history" : "/reading/recent",
              }}
              to={
                "query" in item
                  ? `/search?${item.search}`
                  : readerPath(item.id, item.page)
              }
            >
              {"query" in item ? item.query : item.title}
              <ArrowUpRight size={14} />
            </Link>
            <p>
              {formatTime(item.time)}
              {"page" in item && ` · 第 ${item.page} 页`}
            </p>
          </div>
          <Tooltip title="移除记录">
            <Button
              type="text"
              aria-label={`移除 ${"query" in item ? item.query : item.title}`}
              icon={<Trash2 size={16} />}
              onClick={() => remove(item.id)}
            />
          </Tooltip>
        </article>
      ))}
    </>
  );
}
