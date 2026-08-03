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
    "Practice",
    "Timed exam",
    "Confidence before seeing the result",
    "Confirm answer",
    "Review ledger",
    "Accuracy by area",
    "Question import",
    "Human review queue",
    "Mark reviewed",
    "Publish",
    "Invalidate content",
    "Historical raw score",
    "Content invalidated",
  ]) {
    assert.match(workspace, new RegExp(contract));
  }
  assert.match(workspace, /sessionStorage/);
  assert.match(workspace, /ConfirmDialog/);
  assert.match(workspace, /aria-live="polite"/);
  assert.match(workspace, /openTjmAttemptItem/);
  assert.match(workspace, /TjmCommandLedger/);
  assert.match(workspace, /shouldRefreshExpiredTjmAttempt/);
  assert.match(workspace, /Change answer/);
  assert.doesNotMatch(
    workspace,
    /selected_option_key\s*===\s*correct_option_key/,
  );
});

test("TJM workspace separates official results from personal targets without exam-specific defaults", () => {
  const workspace = source("components/tjm/TjmWorkspace.tsx");
  for (const contract of [
    "Official passing score",
    "Practice target",
    "Official result",
    "Personal target result",
    "Clear target",
    "Scoring source",
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
