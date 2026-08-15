import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";

function source(relativePath: string): string {
  return readFileSync(path.resolve(process.cwd(), relativePath), "utf8");
}

test("TJM workspace exposes every required user and human-review surface", () => {
  const workspace = source("components/tjm/TjmWorkspace.tsx");
  for (const contract of [
    "exam.start.practice",
    "exam.start.timed",
    "attempt.confidence.label",
    "attempt.answer.confirm",
    "review.ledger.title",
    "analytics.area.title",
    "admin.import.title",
    "admin.review.title",
    "admin.review.action.markReviewed",
    "admin.review.action.publish",
    "admin.review.action.invalidate",
    "result.rawScore",
    "attempt.contentInvalidated",
  ]) {
    assert.match(workspace, new RegExp(contract));
  }
  assert.match(workspace, /tjmText/);
  assert.match(workspace, /tjmCodeText/);
  assert.match(workspace, /<main[^>]+lang=\{TJM_LOCALE\}/s);
  assert.doesNotMatch(workspace, /eslint-disable i18n\/no-literal-ui-text/);
  assert.match(workspace, /sessionStorage/);
  assert.match(workspace, /ConfirmDialog/);
  assert.match(workspace, /aria-live="polite"/);
  assert.match(workspace, /openTjmAttemptItem/);
  assert.match(workspace, /TjmCommandLedger/);
  assert.match(workspace, /shouldRefreshExpiredTjmAttempt/);
  assert.match(workspace, /attempt\.answer\.change/);
  assert.doesNotMatch(
    workspace,
    /selected_option_key\s*===\s*correct_option_key/,
  );
});

test("TJM workspace separates official results from personal targets without exam-specific defaults", () => {
  const workspace = source("components/tjm/TjmWorkspace.tsx");
  for (const contract of [
    "exam.field.officialScore",
    "exam.field.practiceTarget",
    "result.dimension.official",
    "result.dimension.practice",
    "exam.target.clear",
    "exam.scoringSource",
  ]) {
    assert.match(workspace, new RegExp(contract));
  }
  assert.match(workspace, /getTjmResultDisplay/);
  assert.match(workspace, /updateTjmExamPreference/);
  assert.match(workspace, /updateTjmOfficialPassingScore/);
  assert.doesNotMatch(workspace, /\bpass_score\b/);
  assert.doesNotMatch(workspace, /takken/i);
  assert.doesNotMatch(workspace, /durationMinutes:\s*["']120["']/);
  assert.doesNotMatch(workspace, /questionCount:\s*["']50["']/);
});

test("TJM has a utility page and a first-class sidebar route", () => {
  assert.match(source("app/(utility)/tjm/page.tsx"), /TjmWorkspace/);
  const sidebar = source("components/sidebar/SidebarShell.tsx");
  assert.match(sidebar, /href: ["']\/tjm["']/);
  assert.match(sidebar, /label: ["']TJM Exam Studio["']/);
});

test("TJM supplies fixed Japanese close copy without changing shared dialog defaults", () => {
  const dialog = source("components/ui/ConfirmDialog.tsx");
  assert.match(dialog, /closeLabel\?: string/);
  assert.match(dialog, /resolvedCloseLabel/);
  assert.match(dialog, /aria-label=\{resolvedCloseLabel\}/);

  const workspace = source("components/tjm/TjmWorkspace.tsx");
  assert.equal(
    workspace.match(/closeLabel=\{tjmText\(["']common\.close["']\)\}/g)?.length,
    2,
  );
});

test("TJM TTS controls are mutually exclusive while audio is loading or speaking", () => {
  const workspace = source("components/tjm/TjmWorkspace.tsx");
  assert.match(workspace, /voice\.state === 'loading'/);
  assert.match(workspace, /voice\.state === 'speaking'/);
  assert.match(workspace, /attempt\.read\.result/);
});
