// The owner's copy rule for an inserted bridge sentence, in one place (owner direction 2026-11-20).
//
// Twelve links went live with sentences that were the draft's internal rationale -- "giving
// readers a ... benchmark" -- because no check stood between a draft and the page. Every applier
// calls this before any CMS request, so the rule holds whichever builder produced the copy, and
// the review dashboard shows the same problems on each card before anyone approves it.
//
// `plain` is the sentence as a visitor reads it, with the link markup already removed.

export const COPY_RULE = Object.freeze({ minWords: 32, maxWords: 38, minSentences: 2 });

const READER_ADDRESS = /\b(readers?|you|your)\b/i;
const DASH = /[–—]|\s-\s/;
// A currency figure in millions or billions carried to two or more decimals: "USD 7.649 million".
const FALSE_PRECISION = /\b(?:USD|US\$|\$)\s?\d+\.\d{2,}\s?(?:million|mn|billion|bn)\b/i;

export const wordCount = (plain) => plain.trim().split(/\s+/).filter(Boolean).length;
export const sentenceCount = (plain) => plain.trim().split(/(?<=[.!?])\s+/).filter(Boolean).length;

export function copyRuleProblems(plain, { readerValue } = {}) {
  const text = String(plain ?? "").replace(/\s+/g, " ").trim();
  const problems = [];
  const words = wordCount(text);
  if (words < COPY_RULE.minWords || words > COPY_RULE.maxWords) problems.push(`${words} words; the rule is ${COPY_RULE.minWords}-${COPY_RULE.maxWords}`);
  const sentences = sentenceCount(text);
  if (sentences < COPY_RULE.minSentences) problems.push(`${sentences} sentence; the rule is at least ${COPY_RULE.minSentences}`);
  if (DASH.test(text)) problems.push("contains a dash");
  if (READER_ADDRESS.test(text)) problems.push("addresses the reader");
  if (FALSE_PRECISION.test(text)) problems.push("carries false precision in a currency figure");
  if (readerValue && text === String(readerValue).replace(/\s+/g, " ").trim()) problems.push("is the internal reader_value rationale, not page copy");
  return problems;
}
