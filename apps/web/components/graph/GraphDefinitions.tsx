import { GRAPH_DEFINITIONS } from "@/lib/graph";

/** What an orphan, a nearly-orphan, a bridge and an entry page are, in words. */
export function GraphDefinitions() {
  return (
    <dl className="grid gap-3 rounded border border-slate-300 bg-slate-50 p-3 text-sm sm:grid-cols-2">
      {GRAPH_DEFINITIONS.map(([term, text]) => (
        <div key={term}>
          <dt className="font-semibold">{term}</dt>
          <dd className="text-slate-800">{text}</dd>
        </div>
      ))}
    </dl>
  );
}
