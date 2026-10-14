import type { AgentStepView, ToolCallRecord } from "@/lib/api-client";
import {
  describeAgentErrorCode,
  describeAgentNode,
  describeModelRole,
  describeStepStatus,
  describeToolName,
  describeToolPermission,
  formatAgentJson,
} from "@/lib/agents";
import { formatDateTime, formatDuration } from "@/lib/format";

import { StatusBadge } from "../StatusBadge";
import { CELL, HeaderRow, NUM_CELL, ROW, TableFrame } from "../inventory/Table";
import { ModelUse } from "./AgentReviewTable";

const FACTS = "grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm";
const TOOL_COLUMNS = ["Tool", "Permission", "Result", "Duration", "Attempt", "Summary"];

function ErrorCode({ code }: { readonly code: string }) {
  return (
    <span className="text-red-900">
      {describeAgentErrorCode(code)} <code className="text-xs">{code}</code>
    </span>
  );
}

function ToolCallTable({ step, calls }: { readonly step: AgentStepView; readonly calls: readonly ToolCallRecord[] }) {
  return (
    <TableFrame caption={`Tool calls of step ${step.seq}`} count={calls.length}>
      <HeaderRow columns={TOOL_COLUMNS} />
      <tbody>
        {calls.map((call, index) => (
          <tr key={`${call.tool}-${index}`} className={ROW}>
            <th scope="row" className={`${CELL} font-medium`}>
              {describeToolName(call.tool)} <code className="block text-xs font-normal text-slate-600">{call.tool}</code>
            </th>
            <td className={CELL}>{describeToolPermission(call.permission)}</td>
            <td className={CELL}>
              {call.ok ? (
                <span className="text-emerald-900">
                  <span aria-hidden="true">✓ </span>OK
                </span>
              ) : (
                <span className="text-red-900">
                  <span aria-hidden="true">✕ </span>Failed
                  {call.error_code !== null && (
                    <span className="block text-xs">
                      <ErrorCode code={call.error_code} />
                    </span>
                  )}
                </span>
              )}
            </td>
            <td className={`${NUM_CELL} whitespace-nowrap`}>{formatDuration(call.duration_ms)}</td>
            <td className={NUM_CELL}>{call.attempt}</td>
            <td className={CELL}>
              <code className="block max-w-xs whitespace-pre-wrap break-all text-xs">{formatAgentJson(call.summary)}</code>
            </td>
          </tr>
        ))}
      </tbody>
    </TableFrame>
  );
}

function Step({ step }: { readonly step: AgentStepView }) {
  const headingId = `step-${step.seq}-heading`;
  return (
    <li aria-labelledby={headingId} className="space-y-3 rounded border border-slate-300 p-3">
      <div className="flex flex-wrap items-center gap-3">
        <h3 id={headingId} className="font-semibold">
          <span className="tabular-nums">{step.seq}.</span> {describeAgentNode(step.node)}{" "}
          <code className="text-xs font-normal text-slate-600">{step.node}</code>
        </h3>
        <StatusBadge presentation={describeStepStatus(step.status)} />
      </div>
      <dl className={FACTS}>
        <dt className="font-medium text-slate-700">Attempt</dt>
        <dd className="tabular-nums">{step.attempt}</dd>
        <dt className="font-medium text-slate-700">Duration</dt>
        <dd>{formatDuration(step.duration_ms)}</dd>
        <dt className="font-medium text-slate-700">Started</dt>
        <dd>
          <time dateTime={step.started_at}>{formatDateTime(step.started_at)}</time>
        </dd>
        <dt className="font-medium text-slate-700">Model role</dt>
        <dd>
          {step.model_role === null ? (
            <span className="text-slate-600">None (deterministic node)</span>
          ) : (
            describeModelRole(step.model_role)
          )}
        </dd>
        {(step.model_role !== null || step.model !== null || step.fallback_used) && (
          <>
            <dt className="font-medium text-slate-700">Model</dt>
            <dd>
              <ModelUse fallbackUsed={step.fallback_used} fallbackReason={step.fallback_reason} model={step.model} />
            </dd>
          </>
        )}
        {step.error_code !== null && (
          <>
            <dt className="font-medium text-red-900">Error code</dt>
            <dd>
              <ErrorCode code={step.error_code} />
            </dd>
          </>
        )}
        <dt className="font-medium text-slate-700">Span id</dt>
        <dd>
          <code className="text-xs">{step.span_id}</code>
        </dd>
      </dl>
      {step.tool_calls.length === 0 ? (
        <p className="text-sm text-slate-700">No tool calls.</p>
      ) : (
        <ToolCallTable step={step} calls={step.tool_calls} />
      )}
      <div className="space-y-1">
        <p id={`step-${step.seq}-output`} className="text-sm font-medium text-slate-700">
          Output
        </p>
        <pre
          tabIndex={0}
          aria-labelledby={`step-${step.seq}-output`}
          className="max-h-48 overflow-auto whitespace-pre-wrap break-all rounded border border-slate-200 bg-slate-50 p-2 text-xs focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-700"
        >
          {formatAgentJson(step.output)}
        </pre>
      </div>
    </li>
  );
}

/**
 * The executed nodes of one run, in `seq` order. Node output and tool summaries
 * are validated, bounded JSON (no raw prompts); they are shown as text only.
 */
export function AgentStepTimeline({ steps }: { readonly steps: readonly AgentStepView[] }) {
  if (steps.length === 0) {
    return (
      <p role="status" className="text-slate-700">
        No steps are recorded for this run.
      </p>
    );
  }
  const ordered = [...steps].sort((a, b) => a.seq - b.seq);
  return (
    <ol aria-label="Step timeline" className="space-y-3">
      {ordered.map((step) => (
        <Step key={step.seq} step={step} />
      ))}
    </ol>
  );
}
