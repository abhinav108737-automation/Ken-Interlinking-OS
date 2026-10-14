"""Phase 5 human review (ADR-0013 sections 4 and 7).

* :mod:`.service`      -- :class:`~.service.DatabaseReviewService`: approve, reject, edit the
  anchor, replace the destination, bulk review; idempotent, version-aware, audited;
* :mod:`.errors`       -- typed refusals with stable reason codes (mapped to 403/409/422);
* :mod:`.reader`       -- :class:`~.reader.SqlReviewReader`: the review-action history;
* :mod:`.controlplane` -- dashboard summary, event log and active-policy views (read-only).
"""
