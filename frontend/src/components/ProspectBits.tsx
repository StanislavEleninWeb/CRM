import { Link } from "react-router-dom";

import type { Schemas } from "../api/client";
import { formatDate, formatDateTime } from "../lib/format";

export type Prospect = Schemas["ProspectOut"];

const REASONS: Record<string, string> = {
  no_permission: "You cannot log calls",
  prospect_inactive: "Not being worked",
  do_not_call: "Do not call",
  no_phone_found: "No phone found",
  no_usable_number: "No number may be called",
  do_not_email: "Do not email",
  no_email_found: "No email found",
  mailbox_not_connected: "Email not connected",
};
const VERIFICATION: Record<string, string> = {
  no_assessment: "No research yet",
  low_confidence: "Low confidence",
  stale_evidence: "Evidence is old",
  finding_contradicted: "Finding contradicted",
  unverified_high_priority: "Not yet verified",
};

export function actionReason(reason: string | null | undefined): string {
  return reason ? (REASONS[reason] ?? reason.replace(/_/g, " ")) : "";
}

export function TierBadge({ prospect, filler }: { prospect: Prospect; filler?: boolean }) {
  if (prospect.total == null) return <span className="badge">Unscored</span>;
  return (
    <span className={`badge tier tier-${prospect.tier}`}>
      Tier {prospect.tier} · {prospect.total}
      {filler ? " · filler" : ""}
    </span>
  );
}

export function VerificationBadges({ prospect }: { prospect: Prospect }) {
  if (prospect.needs_verification.length === 0) return null;
  return (
    <>
      {prospect.needs_verification.map((reason) => (
        <span key={reason} className="badge badge-warn">
          {VERIFICATION[reason] ?? reason.replace(/_/g, " ")}
        </span>
      ))}
    </>
  );
}

/** One prospect as a card. Used on phones for the list and everywhere for the call queue. */
export function ProspectCard({
  prospect,
  rank,
  filler,
  reason,
  timeZone,
}: {
  prospect: Prospect;
  rank?: number;
  filler?: boolean;
  reason?: string | null;
  timeZone?: string;
}) {
  const call = prospect.actions.call;
  return (
    <li className="prospect-card">
      <div className="prospect-head">
        <Link className="row-title" to={`/prospects/${prospect.lead_id}`}>
          {rank ? `${rank}. ` : ""}
          {prospect.company_name}
        </Link>
        <TierBadge prospect={prospect} filler={filler} />
      </div>
      <span className="muted">
        {[prospect.business_type, prospect.city, prospect.external_id].filter(Boolean).join(" · ")}
      </span>
      {reason === "follow_up_due" ? (
        <span className="badge badge-warn">
          Follow-up due {formatDateTime(prospect.next_follow_up_at, timeZone)}
        </span>
      ) : null}
      {prospect.finding ? <p className="finding">{prospect.finding}</p> : null}
      <div className="badges">
        <span className="badge">Confidence: {prospect.confidence}</span>
        <span className="badge">Checked {formatDate(prospect.checked_on)}</span>
        {prospect.service_category ? <span className="badge">{prospect.service_category}</span> : null}
        <VerificationBadges prospect={prospect} />
        {call?.available ? null : <span className="badge badge-warn">{actionReason(call?.reason)}</span>}
        {prospect.email_count === 0 ? <span className="badge">No email found</span> : null}
      </div>
    </li>
  );
}
