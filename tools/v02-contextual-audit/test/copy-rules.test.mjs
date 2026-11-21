import assert from "node:assert/strict";
import test from "node:test";
import { copyRuleProblems } from "../copy-rules.mjs";

// The live rewrite of cluster:3: the shape every inserted sentence must have.
const GOOD = "The Malaysia Guaiacol Market grows at 6.5% a year to about USD 6 million by 2032, ahead of Kuwait's 5.0%. Pharmaceutical manufacturing reaches 46% of modelled spending, while purchase prices are expected to stay broadly flat.";

test("a compliant sentence has no problems", () => {
  assert.deepEqual(copyRuleProblems(GOOD), []);
});

test("the published defect is refused on every count it broke", () => {
  // cluster:11 as it first went live (deployment 51).
  const live = "The Qatar Orthopedic Spine Navigation Systems Market reaches USD 1.885 million by 2032 at 8.0% CAGR, compared with Malaysia's USD 24 million and 11.1%, giving readers a smaller hospital-procurement benchmark.";
  const problems = copyRuleProblems(live, { readerValue: live });
  assert.ok(problems.some((p) => p.includes("sentence")), "one sentence");
  assert.ok(problems.includes("addresses the reader"));
  assert.ok(problems.includes("carries false precision in a currency figure"));
  assert.ok(problems.includes("is the internal reader_value rationale, not page copy"));
});

test("word bounds are inclusive at 32 and 38", () => {
  // Exactly n words across two sentences.
  const words = (n) => Array.from({ length: n - 1 }, () => "word").join(" ") + ". End.";
  const wordProblem = (n) => copyRuleProblems(words(n)).some((p) => p.includes("words"));
  assert.equal(wordProblem(32), false);
  assert.equal(wordProblem(38), false);
  assert.equal(wordProblem(31), true);
  assert.equal(wordProblem(39), true);
});

test("em and en dashes are refused; a hyphenated word is not", () => {
  assert.ok(copyRuleProblems(GOOD.replace(", ahead", " — ahead")).includes("contains a dash"));
  assert.ok(copyRuleProblems(GOOD.replace(", ahead", " – ahead")).includes("contains a dash"));
  assert.deepEqual(copyRuleProblems(GOOD.replace("Pharmaceutical", "Drug-grade")), []);
});

test("one decimal in a currency figure is ordinary rounding, not false precision", () => {
  assert.equal(copyRuleProblems(GOOD.replace("USD 6 million", "USD 6.1 million")).includes("carries false precision in a currency figure"), false);
  assert.ok(copyRuleProblems(GOOD.replace("USD 6 million", "USD 6.12 million")).includes("carries false precision in a currency figure"));
});
