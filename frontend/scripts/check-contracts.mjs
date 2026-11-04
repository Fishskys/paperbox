import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const root = fileURLToPath(new URL("..", import.meta.url));
const schemas = JSON.parse(execFileSync("uv", ["run", "python", "scripts/export_webui_contracts.py"], { cwd: resolve(root, ".."), encoding: "utf8" }));
function type(schema, response = true) {
  if (schema.$ref) return `Api_${schema.$ref.split("/").at(-1)}`;
  if (schema.const !== undefined) return JSON.stringify(schema.const);
  if (schema.enum) return schema.enum.map(value => JSON.stringify(value)).join(" | ");
  if (schema.anyOf || schema.oneOf) return `(${(schema.anyOf || schema.oneOf).map(item => type(item, response)).join(" | ")})`;
  if (schema.allOf) return `(${schema.allOf.map(item => type(item, response)).join(" & ")})`;
  if (schema.type === "array") return `Array<${type(schema.items, response)}>`;
  if (schema.type === "object" || schema.properties) {
    if (!schema.properties) return `Record<string, ${schema.additionalProperties && schema.additionalProperties !== true ? type(schema.additionalProperties, response) : "unknown"}>`;
    return `{ ${Object.entries(schema.properties).map(([key, value]) => `${JSON.stringify(key)}${response || schema.required?.includes(key) ? "" : "?"}: ${type(value, response)}`).join("; ")} }`;
  }
  return ({ string: "string", number: "number", integer: "number", boolean: "boolean", null: "null" })[schema.type] || "unknown";
}
const pairs = { Paper: "PaperOut", Job: "JobOut", JobList: "JobListOut", QueueSnapshot: "QueueOut", AuthContext: "AuthContextOut", ReextractReport: "ReextractOut", MaintenanceJob: "MaintenanceOut", BatchMaintenanceReport: "MaintenanceReportOut" };
let source = 'import type * as Contracts from "./src/contracts";\ntype Assert<T extends true> = T;\n';
source += Object.entries(schemas).map(([name, schema]) => `type Api_${name} = ${type(schema)};`).join("\n");
for (const [client, server] of Object.entries(pairs)) {
  if (!schemas[server]) throw new Error(`Missing OpenAPI schema ${server}`);
  source += `\ntype Check_${client} = Assert<{ [K in keyof Contracts.${client}]-?: K extends keyof Api_${server} ? NonNullable<Api_${server}[K]> extends NonNullable<Contracts.${client}[K]> ? true : false : false }[keyof Contracts.${client}] extends true ? true : false>;`;
}
source += `\ntype RequestShape = ${type(schemas.MaintenanceIn, false)};\nconst request: Omit<RequestShape, "request_id"> = {} as Contracts.MaintenanceRequest;\nvoid request;`;
const virtual = resolve(root, "contract-check.ts");
const options = { strict: true, noEmit: true, skipLibCheck: true, target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, moduleResolution: ts.ModuleResolutionKind.Bundler };
const host = ts.createCompilerHost(options);
const original = host.getSourceFile;
host.getSourceFile = (file, language, ...args) => file === virtual ? ts.createSourceFile(file, source, language, true) : original(file, language, ...args);
const program = ts.createProgram([virtual], options, host);
const errors = ts.getPreEmitDiagnostics(program);
if (errors.length) {
  process.stderr.write(ts.formatDiagnosticsWithColorAndContext(errors, { getCanonicalFileName: value => value, getCurrentDirectory: () => root, getNewLine: () => "\n" }));
  process.exitCode = 1;
} else console.info(`WebUI contracts passed (${Object.keys(pairs).length} responses and maintenance request).`);
