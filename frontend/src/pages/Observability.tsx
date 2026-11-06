import { Tabs } from "antd";
import { useSearchParams } from "react-router-dom";
import { PageHeader } from "../components";
import Status from "./Status";
import SystemLogs from "./SystemLogs";

export default function Observability() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "logs" ? "logs" : "status";
  return <>
    <PageHeader title="数据与日志" />
    <Tabs activeKey={tab} destroyOnHidden onChange={value => setParams(value === "logs" ? { tab: "logs" } : {})}
      items={[{ key: "status", label: "状态", children: <Status /> }, { key: "logs", label: "日志", children: <SystemLogs /> }]} />
  </>;
}
