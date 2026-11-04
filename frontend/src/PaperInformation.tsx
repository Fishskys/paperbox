import type { ReactNode } from "react";
import type { Paper } from "./contracts";
import CitationActions from "./CitationActions";

const types: Record<string, string> = {
  journal: "期刊论文", conference: "会议论文", preprint: "预印本",
  early_access: "在线优先", standard: "标准",
};

export default function PaperInformation({ paper, actions }: { paper: Paper; actions?: ReactNode }) {
  return <article className="paper-info">
    <header className="paper-info-heading">
      <h2>{paper.title}</h2>
      <dl className="paper-info-authors">
        <dt>作者</dt>
        <dd>{paper.authors.length ? <ul>{paper.authors.map((author, index) => <li key={`${index}-${author}`}>{author}</li>)}</ul> : "未标注"}</dd>
      </dl>
    </header>
    <section aria-label="出版信息">
      <h3>出版信息</h3>
      <dl className="paper-info-fields">
        <div className="paper-info-wide"><dt>期刊 / 会议</dt><dd>{paper.venue || "未标注"}</dd></div>
        <div><dt>发表年份</dt><dd>{paper.year || "未标注"}</dd></div>
        <div><dt>文献类型</dt><dd>{paper.paper_type ? types[paper.paper_type] || paper.paper_type : "未标注"}</dd></div>
        {paper.volume && <div><dt>卷号</dt><dd>{paper.volume}</dd></div>}
        {paper.issue && <div><dt>期号</dt><dd>{paper.issue}</dd></div>}
        {paper.pages && <div><dt>页码</dt><dd>{paper.pages}</dd></div>}
        <div className="paper-info-wide"><dt>DOI</dt><dd>{paper.doi || "未标注"}</dd></div>
      </dl>
    </section>
    <section aria-label="摘要"><h3>摘要</h3><p>{paper.abstract || "暂无摘要"}</p></section>
    <section aria-label="引用"><h3>引用</h3><CitationActions paperId={paper.paper_id} /></section>
    {actions && <footer className="paper-info-actions">{actions}</footer>}
  </article>;
}
