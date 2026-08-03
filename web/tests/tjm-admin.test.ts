import test from "node:test";
import assert from "node:assert/strict";

import {
  adminReviewActions,
  selectAdminReviewQuestions,
} from "../lib/tjm-admin";
import type { TjmQuestionVersion } from "../lib/tjm-types";

function question(
  id: string,
  status: TjmQuestionVersion["status"],
  review_binding_state: TjmQuestionVersion["review_binding_state"],
): TjmQuestionVersion {
  return { id, status, review_binding_state } as TjmQuestionVersion;
}

test("admin queue includes drafts and only legacy-unverified publications", () => {
  const draft = question("draft", "draft", "unreviewed");
  const legacy = question("legacy", "published", "legacy_unverified");
  const current = question("current", "published", "current");

  assert.deepEqual(selectAdminReviewQuestions([draft], [legacy, current]), [draft, legacy]);
});

test("legacy publications expose only the re-review action", () => {
  assert.deepEqual(adminReviewActions(question("legacy", "published", "legacy_unverified")), {
    canEdit: false,
    canReview: true,
    canPublish: false,
    canReject: false,
  });
});

test("reviewed drafts expose publish, edit, and reject without duplicate review", () => {
  assert.deepEqual(adminReviewActions(question("draft", "draft", "current")), {
    canEdit: true,
    canReview: false,
    canPublish: true,
    canReject: true,
  });
});
