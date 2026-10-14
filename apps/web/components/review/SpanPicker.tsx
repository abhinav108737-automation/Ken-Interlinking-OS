"use client";

import { type KeyboardEvent, useId, useMemo, useRef, useState } from "react";

import { codePointLength } from "@/lib/recommendations";
import { sliceCodePoints, spanOfWords, tokenizeWords, validateSpan, wordsOf } from "@/lib/review";

import { HighlightedAnchor } from "../recommendations/PlacementPreview";
import { FieldError } from "./fields";

export interface SpanPickerProps {
  /** The plan's block text, verbatim. Rendered as text only. */
  readonly text: string;
  readonly initialStart: number;
  readonly initialEnd: number;
  /** Server-side error for the span, if the last submit was refused. */
  readonly error?: string | undefined;
}

/**
 * Pick a new anchor as a span of the block text, by word boundaries.
 *
 * - Click (or Enter/Space on) the first word, then the last word. Arrow keys,
 *   Home and End move between words (one tab stop for the whole block).
 * - Or type code-point offsets directly; the preview shows the exact slice.
 *
 * Offsets are Unicode code points, as the API counts them. The new anchor is
 * always a verbatim slice of `text`; nothing here composes or rewrites text.
 * Submits `start` and `end` as hidden fields.
 */
export function SpanPicker({ text, initialStart, initialEnd, error }: SpanPickerProps) {
  const id = useId();
  const pieces = useMemo(() => tokenizeWords(text), [text]);
  const words = useMemo(() => wordsOf(pieces), [pieces]);
  const length = codePointLength(text);
  const [start, setStart] = useState(initialStart);
  const [end, setEnd] = useState(initialEnd);
  const [pendingFirst, setPendingFirst] = useState<number | null>(null);
  const [focusIndex, setFocusIndex] = useState(() => {
    const index = words.findIndex((word) => word.end > initialStart);
    return index === -1 ? 0 : index;
  });
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);

  const problem = validateSpan(text, start, end);
  const slice = sliceCodePoints(text, start, end);
  const changed = start !== initialStart || end !== initialEnd;

  function choose(index: number) {
    if (pendingFirst === null) {
      const span = spanOfWords(words, index, index);
      if (span === null) return;
      setStart(span.start);
      setEnd(span.end);
      setPendingFirst(index);
    } else {
      const span = spanOfWords(words, pendingFirst, index);
      if (span === null) return;
      setStart(span.start);
      setEnd(span.end);
      setPendingFirst(null);
    }
  }

  function move(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const target =
      event.key === "ArrowRight" || event.key === "ArrowDown"
        ? Math.min(index + 1, words.length - 1)
        : event.key === "ArrowLeft" || event.key === "ArrowUp"
          ? Math.max(index - 1, 0)
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? words.length - 1
              : null;
    if (target === null) return;
    event.preventDefault();
    setFocusIndex(target);
    buttons.current[target]?.focus();
  }

  function setOffset(which: "start" | "end", raw: string) {
    const value = Number.parseInt(raw, 10);
    if (!Number.isSafeInteger(value)) return;
    setPendingFirst(null);
    if (which === "start") setStart(value);
    else setEnd(value);
  }

  const instructionsId = `${id}-instructions`;
  const previewId = `${id}-preview`;
  const errorId = `${id}-error`;
  const shownError = problem ?? error;

  return (
    <fieldset className="space-y-3" aria-describedby={instructionsId}>
      <legend className="font-medium text-slate-800">New anchor span</legend>
      <p id={instructionsId} className="text-sm text-slate-700">
        Choose the first word of the new anchor, then the last word (click, or press Enter). Arrow keys move between
        words. Or type the character offsets below. The anchor is always copied verbatim from this text.
      </p>
      <div
        role="group"
        aria-label="Words of the source block"
        className="max-h-64 overflow-auto rounded border border-slate-300 bg-slate-50 p-3 text-sm leading-loose"
      >
        <p className="whitespace-pre-wrap break-words">
          {pieces.map((piece, position) => {
            if (piece.word === null) return <span key={`s${position}`}>{piece.text}</span>;
            const word = piece.word;
            const selected = word.start >= start && word.end <= end;
            return (
              <button
                key={`w${word.index}`}
                ref={(element) => {
                  buttons.current[word.index] = element;
                }}
                type="button"
                tabIndex={word.index === focusIndex ? 0 : -1}
                aria-pressed={selected}
                onClick={() => {
                  setFocusIndex(word.index);
                  choose(word.index);
                }}
                onKeyDown={(event) => move(event, word.index)}
                className={`rounded-sm px-px focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-700 ${
                  selected ? "bg-yellow-200 font-semibold text-slate-950 underline" : "hover:bg-slate-200"
                }${pendingFirst === word.index ? " ring-2 ring-blue-700" : ""}`}
              >
                {piece.text}
              </button>
            );
          })}
        </p>
      </div>
      {pendingFirst !== null && (
        <p className="text-sm text-slate-800" role="status">
          First word chosen. Now choose the last word of the anchor (or the same word again).
        </p>
      )}
      <div className="flex flex-wrap items-end gap-3 text-sm">
        <div className="flex flex-col gap-1">
          <label htmlFor={`${id}-start`} className="font-medium text-slate-800">
            Start offset
          </label>
          <input
            id={`${id}-start`}
            type="number"
            inputMode="numeric"
            min={0}
            max={Math.max(length - 1, 0)}
            step={1}
            value={start}
            onChange={(event) => setOffset("start", event.target.value)}
            aria-describedby={previewId}
            className="w-28 rounded border border-slate-400 bg-white px-2 py-1"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor={`${id}-end`} className="font-medium text-slate-800">
            End offset (exclusive)
          </label>
          <input
            id={`${id}-end`}
            type="number"
            inputMode="numeric"
            min={1}
            max={length}
            step={1}
            value={end}
            onChange={(event) => setOffset("end", event.target.value)}
            aria-describedby={previewId}
            className="w-28 rounded border border-slate-400 bg-white px-2 py-1"
          />
        </div>
        <p className="text-xs text-slate-700">Offsets count characters (Unicode code points) of the block text, 0–{length}.</p>
      </div>
      <input type="hidden" name="start" value={start} />
      <input type="hidden" name="end" value={end} />
      <output id={previewId} aria-live="polite" className="block text-sm">
        {slice === null ? (
          <span className="text-slate-700">No valid span selected.</span>
        ) : (
          <>
            New anchor: “<span className="font-semibold">{slice}</span>”{" "}
            <span className="tabular-nums text-slate-700">
              ({start}–{end}){changed ? "" : " — unchanged"}
            </span>
          </>
        )}
      </output>
      <FieldError id={errorId} message={shownError} />
      {slice !== null && problem === null && (
        <figure className="space-y-1 rounded border border-slate-300 bg-white p-3">
          <figcaption className="text-sm font-semibold text-slate-900">After (with the edited link)</figcaption>
          <HighlightedAnchor
            before={Array.from(text).slice(0, start).join("")}
            anchor={slice}
            after={Array.from(text).slice(end).join("")}
            linked
          />
        </figure>
      )}
    </fieldset>
  );
}
