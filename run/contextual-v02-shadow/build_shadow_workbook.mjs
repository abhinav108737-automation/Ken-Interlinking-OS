import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const sourceCsv = "C:/Users/Abhinav/OneDrive - Ken Research Private Limited/Desktop/v02_reports_test_interlinking.csv";
const reviewMd = "C:/Projects/Ken-Interlinking-OS/docs/evaluation/V02_KUWAIT_CLOUD_MANUAL_INTERLINK_REVIEW.md";
const outputDir = "C:/Projects/Ken-Interlinking-OS/outputs/contextual-v02-shadow";
const previewDir = "C:/Projects/Ken-Interlinking-OS/run/contextual-v02-shadow/previews";
const outputPath = `${outputDir}/v02_contextual_interlinking_shadow_review.xlsx`;
const dashboardDataDir = "C:/Projects/Ken-Interlinking-OS/data/v02-contextual-review";
const dashboardDataPath = `${dashboardDataDir}/proposals.json`;

function cleanText(value) {
  return String(value ?? "")
    .replaceAll("â€œ", "“")
    .replaceAll("â€", "”")
    .replaceAll("â€¦", "…")
    .replaceAll("â€”", "—")
    .replaceAll("â€“", "–")
    .replaceAll("â€™", "’")
    .replaceAll("**", "")
    .replaceAll("`", "")
    .trim();
}

// The current Markdown source is valid UTF-8. Keep its smart punctuation intact;
// the older compatibility cleaner above is retained only for historical inputs.
function cleanSourceText(value) {
  return String(value ?? "").replaceAll("**", "").replaceAll("`", "").trim();
}

function parseMarkdownRow(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(cleanSourceText);
}

function tableBetween(markdown, startMarker, endMarker) {
  const start = markdown.indexOf(startMarker);
  const end = markdown.indexOf(endMarker, start + startMarker.length);
  if (start < 0 || end < 0) throw new Error(`Missing Markdown section: ${startMarker}`);
  return markdown
    .slice(start + startMarker.length, end)
    .split(/\r?\n/)
    .filter((line) => /^\|\s*\d+\s*\|/.test(line))
    .map(parseMarkdownRow);
}

function parseLink(cell) {
  const match = cell.match(/^\[([^\]]+)\]\((https?:\/\/[^)]+)\)$/);
  if (!match) return { name: cleanSourceText(cell), url: "" };
  return { name: cleanSourceText(match[1]), url: match[2] };
}

function normalizedCore(value) {
  return String(value ?? "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function placementPolicy(placement, mode) {
  const text = `${placement} ${mode}`.toLowerCase().replaceAll("-", "_");
  const reasons = [];
  if (text.includes("market_overview") || text.includes("market overview")) {
    reasons.push("Market Overview is reserved for the source market.");
  }
  if (text.includes("related_report") || text.includes("related report")) {
    reasons.push("Related Reports already lists reports; no second interlinking surface is allowed.");
  }
  return { eligible: reasons.length === 0, reasons };
}

const csvText = await fs.readFile(sourceCsv, "utf8");
const sourceWorkbook = await Workbook.fromCSV(csvText, { sheetName: "Inventory" });
const sourceSheet = sourceWorkbook.worksheets.getItem("Inventory");
const inventoryValues = sourceSheet.getUsedRange(true).values;
const headers = inventoryValues[0].map((value) => String(value));
const dataRows = inventoryValues.slice(1);
const index = Object.fromEntries(headers.map((name, i) => [name, i]));

const markdown = await fs.readFile(reviewMd, "utf8");
const appliedRows = tableBetween(markdown, "## Applied test links", "## Recommended links");
const applied = new Map(appliedRows.map((row) => [Number(row[0]), { deployment: row[1], status: row[2] }]));
const recommendationRows = tableBetween(markdown, "## Recommended links", "## Machine matches that should be rejected");

const reviewRows = recommendationRows.map((row) => {
  const id = Number(row[0]);
  const source = parseLink(row[2]);
  const destination = parseLink(row[3]);
  const live = applied.get(id);
  const mode = row[1];
  const changeClass = /sentence insertion|copy edit/i.test(mode)
    ? "Context edit"
    : /override/i.test(mode)
      ? "Link with editorial override"
      : "Link existing text";
  return [
    id,
    live ? "Live - verified" : "Shadow - pending",
    "",
    changeClass,
    mode,
    source.name,
    destination.name,
    row[4],
    row[5],
    row[6],
    source.url,
    destination.url,
    live?.deployment ?? "",
    "",
  ];
});

const groups = new Map();
for (const row of dataRows) {
  const core = normalizedCore(row[index.market_core]);
  if (!core) continue;
  if (!groups.has(core)) groups.set(core, []);
  groups.get(core).push(row);
}

const regionalRows = [];
for (const [core, rows] of groups) {
  const geographies = new Set(rows.map((row) => String(row[index.geography] ?? "")));
  if (rows.length < 2 || geographies.size < 2) continue;
  for (const source of rows) {
    for (const destination of rows) {
      if (source === destination) continue;
      if (source[index.geography] === destination[index.geography]) continue;
      regionalRows.push([
        core,
        source[index.geography],
        source[index.title],
        destination[index.geography],
        destination[index.title],
        "Excluded - requires allowed inline context",
        "Eligible only when allowed explanatory prose names or compares the destination geography and states the reader benefit.",
        source[index.url],
        destination[index.url],
        "",
        "",
      ]);
    }
  }
}
regionalRows.sort((a, b) => String(a[0]).localeCompare(String(b[0])) || String(a[1]).localeCompare(String(b[1])));

const workbook = Workbook.create();
const summary = workbook.worksheets.add("Summary");
const review = workbook.worksheets.add("Review");
const regional = workbook.worksheets.add("Regional siblings");
const inventory = workbook.worksheets.add("Inventory");

const font = "Arial";
const burgundy = "#8B1E2D";
const dark = "#242424";
const lightGray = "#E7E7E7";
const softGray = "#F5F5F5";
const amber = "#FFF2CC";
const green = "#E2F0D9";
const red = "#FCE4D6";

for (const sheet of [summary, review, regional, inventory]) {
  sheet.showGridLines = false;
}

summary.tabColor = burgundy;
review.tabColor = "#B74B5B";
regional.tabColor = "#D6A657";

summary.getRange("A2:H2").merge();
summary.getRange("A2").values = [["v0.2 contextual interlinking shadow review"]];
summary.getRange("A2").format.font = { name: font, size: 15, bold: true, color: dark };
summary.getRange("A3:H3").format.borders = { bottom: { style: "thin", color: burgundy } };
summary.getRange("A5:B5").values = [["Current v0.2 inventory", "Count"]];
summary.getRange("A6:A14").values = [
  ["Reports"],
  ["Kuwait reports"],
  ["Kuwait Cloud-Based pilot reports"],
  ["Reviewed contextual proposals"],
  ["Live and verified proposals"],
  ["Pending shadow proposals"],
  ["Exact regional market groups"],
  ["Regional sibling evidence pairs"],
  ["Reports missing market_id"],
];
const inventoryEnd = dataRows.length + 1;
const reviewEnd = reviewRows.length + 1;
const regionalEnd = regionalRows.length + 1;
const snapshotCounts = [
  dataRows.length,
  dataRows.filter((row) => String(row[index.geography] ?? "").toLowerCase() === "kuwait").length,
  dataRows.filter((row) => String(row[index.slug] ?? "").toLowerCase().startsWith("kuwait-cloud-based-")).length,
  reviewRows.length,
  reviewRows.filter((row) => row[1].startsWith("Live")).length,
  reviewRows.filter((row) => row[1].startsWith("Shadow")).length,
  new Set(regionalRows.map((row) => row[0])).size,
  regionalRows.length,
  dataRows.filter((row) => !String(row[index.market_id] ?? "").trim()).length,
];
// These are intentionally stored snapshot values. The export engine does not
// reliably cache COUNTIF results, so formulas made the workbook open with zeros.
summary.getRange("B6:B14").values = snapshotCounts.map((value) => [value]);
summary.getRange("D5:H5").merge();
summary.getRange("D5").values = [["Review workflow"]];
summary.getRange("D6:H11").values = [
  ["1", "Check source and destination relevance", null, null, null],
  ["2", "Review exact wording and visible placement", null, null, null],
  ["3", "Choose Approve, Revise or Reject", null, null, null],
  ["4", "Dry-run approved content changes", null, null, null],
  ["5", "Apply only a five-page canary", null, null, null],
  ["6", "Verify the public rendered anchor and record rollback ID", null, null, null],
];
summary.getRange("D13:H15").merge();
summary.getRange("D13").values = [[
  "Related Reports modules are disabled. A regional sibling must earn a natural placement in allowed explanatory prose or receive no link."
]];
summary.getRange("D13:H15").format = { fill: softGray, font: { name: font, size: 10, italic: true, color: dark }, wrapText: true, verticalAlignment: "center" };

const reviewHeaders = [
  "ID", "Current status", "Review decision", "Change class", "Mode", "Source report",
  "Destination report", "Field and placement", "Anchor or proposed wording", "Reader value and reason",
  "Source URL", "Destination URL", "Deployment ID", "Reviewer notes",
];
review.getRangeByIndexes(0, 0, 1, reviewHeaders.length).values = [reviewHeaders];
review.getRangeByIndexes(1, 0, reviewRows.length, reviewHeaders.length).values = reviewRows;
review.getRange(`C2:C${reviewEnd}`).dataValidation = { rule: { type: "list", values: ["Approve", "Revise", "Reject"] } };
review.freezePanes.freezeRows(1);
review.freezePanes.freezeColumns(3);

const regionalHeaders = [
  "Market core", "Source geography", "Source report", "Destination geography", "Destination report",
  "Recommended surface", "Inline condition", "Source URL", "Destination URL", "Review decision", "Reviewer notes",
];
regional.getRangeByIndexes(0, 0, 1, regionalHeaders.length).values = [regionalHeaders];
regional.getRangeByIndexes(1, 0, regionalRows.length, regionalHeaders.length).values = regionalRows;
regional.getRange(`J2:J${regionalEnd}`).dataValidation = { rule: { type: "list", values: ["Approve module", "Review inline", "Reject"] } };
regional.freezePanes.freezeRows(1);
regional.freezePanes.freezeColumns(2);

inventory.getRangeByIndexes(0, 0, inventoryValues.length, inventoryValues[0].length).values = inventoryValues;
inventory.freezePanes.freezeRows(1);
inventory.freezePanes.freezeColumns(3);

function styleHeader(sheet, range) {
  sheet.getRange(range).format = {
    fill: burgundy,
    font: { name: font, size: 10, bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "inside", style: "thin", color: "#FFFFFF" },
  };
}

styleHeader(summary, "A5:B5");
styleHeader(summary, "D5:H5");
styleHeader(review, `A1:N1`);
styleHeader(regional, `A1:K1`);
styleHeader(inventory, `A1:G1`);

summary.getRange("A6:B14").format.borders = { insideHorizontal: { style: "thin", color: lightGray } };
summary.getRange("B6:B14").format.numberFormat = "0";
summary.getRange("A1:H16").format.font = { name: font, size: 10, color: dark };
summary.getRange("A2").format.font = { name: font, size: 15, bold: true, color: dark };
summary.getRange("A5:B5").format.font = { name: font, size: 10, bold: true, color: "#FFFFFF" };
summary.getRange("D5:H5").format.font = { name: font, size: 10, bold: true, color: "#FFFFFF" };
summary.getRange("D6:D11").format.font = { name: font, size: 10, bold: true, color: burgundy };
summary.getRange("E6:H11").merge(true);

for (const [sheet, bodyRange] of [
  [review, `A2:N${reviewEnd}`],
  [regional, `A2:K${regionalEnd}`],
  [inventory, `A2:G${inventoryEnd}`],
]) {
  sheet.getRange(bodyRange).format = {
    font: { name: font, size: 10, color: dark },
    verticalAlignment: "top",
    borders: { insideHorizontal: { style: "thin", color: lightGray } },
  };
}
review.getRange(`D2:J${reviewEnd}`).format.wrapText = true;
review.getRange(`N2:N${reviewEnd}`).format.wrapText = true;
regional.getRange(`C2:G${regionalEnd}`).format.wrapText = true;
regional.getRange(`K2:K${regionalEnd}`).format.wrapText = true;

review.getRange(`B2:B${reviewEnd}`).conditionalFormats.add("containsText", { text: "Live", format: { fill: green, font: { color: "#375623", bold: true } } });
review.getRange(`B2:B${reviewEnd}`).conditionalFormats.add("containsText", { text: "Shadow", format: { fill: amber, font: { color: "#7F6000", bold: true } } });
review.getRange(`C2:C${reviewEnd}`).conditionalFormats.add("containsText", { text: "Approve", format: { fill: green, font: { color: "#375623", bold: true } } });
review.getRange(`C2:C${reviewEnd}`).conditionalFormats.add("containsText", { text: "Revise", format: { fill: amber, font: { color: "#7F6000", bold: true } } });
review.getRange(`C2:C${reviewEnd}`).conditionalFormats.add("containsText", { text: "Reject", format: { fill: red, font: { color: "#9C0006", bold: true } } });

summary.getRange("A:A").format.columnWidth = 34;
summary.getRange("B:B").format.columnWidth = 12;
summary.getRange("C:C").format.columnWidth = 3;
summary.getRange("D:D").format.columnWidth = 7;
summary.getRange("E:H").format.columnWidth = 18;

const reviewWidths = [7, 18, 17, 21, 24, 28, 28, 35, 52, 52, 44, 44, 14, 30];
reviewWidths.forEach((width, col) => review.getRangeByIndexes(0, col, reviewEnd, 1).format.columnWidth = width);
review.getRange("A:C").format.verticalAlignment = "center";

const regionalWidths = [24, 16, 34, 20, 34, 23, 50, 44, 44, 18, 30];
regionalWidths.forEach((width, col) => regional.getRangeByIndexes(0, col, regionalEnd, 1).format.columnWidth = width);

const inventoryWidths = [19, 55, 48, 18, 23, 40, 50];
inventoryWidths.forEach((width, col) => inventory.getRangeByIndexes(0, col, inventoryEnd, 1).format.columnWidth = width);

review.getRange(`A1:N${reviewEnd}`).format.autofitRows();
regional.getRange(`A1:K${regionalEnd}`).format.autofitRows();
inventory.getRange("A1:G30").format.autofitRows();
summary.getRange("A1:H16").format.autofitRows();

workbook.recalculate();
await fs.mkdir(outputDir, { recursive: true });
await fs.mkdir(previewDir, { recursive: true });
await fs.mkdir(dashboardDataDir, { recursive: true });

const dashboardData = {
  schema_version: 1,
  dataset: "v0.2",
  editorial_policy: "owner-2026-11-20",
  generated_at: new Date().toISOString(),
  inventory_count: dataRows.length,
  proposals: [
    ...reviewRows.map((row) => {
      const assessment = placementPolicy(row[7], row[4]);
      return {
      id: `contextual:${row[0]}`,
      kind: "contextual",
      current_status: row[1],
      change_class: row[3],
      mode: row[4],
      source_report: row[5],
      destination_report: row[6],
      placement: row[7],
      proposed_wording: row[8],
      reader_value: row[9],
      source_url: row[10],
      destination_url: row[11],
      deployment_id: row[12] || null,
      next_step: "contextual_canary",
      policy_eligible: assessment.eligible,
      policy_reasons: assessment.reasons,
    }; }),
    ...regionalRows.map((row, offset) => ({
      id: `regional:${offset + 1}`,
      kind: "regional",
      current_status: "Shadow - pending",
      change_class: "Regional sibling",
      mode: row[5],
      market_core: row[0],
      source_geography: row[1],
      source_report: row[2],
      destination_geography: row[3],
      destination_report: row[4],
      placement: row[5],
      proposed_wording: row[4],
      reader_value: `Exact same market core (${row[0]}) in a different geography. ${row[6]}`,
      source_url: row[7],
      destination_url: row[8],
      deployment_id: null,
      next_step: "regional_inline_evidence_review",
      policy_eligible: false,
      policy_reasons: ["Related Reports modules are disabled; a regional sibling must earn an allowed inline placement."],
    })),
  ],
};
await fs.writeFile(dashboardDataPath, JSON.stringify(dashboardData, null, 2) + "\n", "utf8");

const checks = {};
checks.summary = (await workbook.inspect({ kind: "table", range: "Summary!A1:H16", include: "values,formulas", tableMaxRows: 20, tableMaxCols: 10 })).ndjson;
checks.review = (await workbook.inspect({ kind: "table", range: `Review!A1:N${reviewEnd}`, include: "values,formulas", tableMaxRows: 18, tableMaxCols: 14, tableMaxCellChars: 140 })).ndjson;
checks.regional = (await workbook.inspect({ kind: "table", range: `'Regional siblings'!A1:K${regionalEnd}`, include: "values,formulas", tableMaxRows: 22, tableMaxCols: 11, tableMaxCellChars: 120 })).ndjson;
checks.errors = (await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!", options: { useRegex: true, maxResults: 300 }, summary: "final formula error scan" })).ndjson;
await fs.writeFile(`${previewDir}/inspection.json`, JSON.stringify(checks, null, 2), "utf8");

for (const [sheetName, range] of [
  ["Summary", "A1:H16"],
  ["Review", `A1:N${reviewEnd}`],
  ["Regional siblings", `A1:K${regionalEnd}`],
  ["Inventory", "A1:G30"],
]) {
  const preview = await workbook.render({ sheetName, range, scale: 1, format: "png" });
  await fs.writeFile(`${previewDir}/${sheetName.toLowerCase().replaceAll(" ", "-")}.png`, new Uint8Array(await preview.arrayBuffer()));
}

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);

console.log(JSON.stringify({
  outputPath,
  inventoryReports: dataRows.length,
  contextualProposals: reviewRows.length,
  liveProposals: reviewRows.filter((row) => row[1].startsWith("Live")).length,
  pendingProposals: reviewRows.filter((row) => row[1].startsWith("Shadow")).length,
  regionalGroups: new Set(regionalRows.map((row) => row[0])).size,
  regionalDirections: regionalRows.length,
  checksPath: `${previewDir}/inspection.json`,
  dashboardDataPath,
}, null, 2));
