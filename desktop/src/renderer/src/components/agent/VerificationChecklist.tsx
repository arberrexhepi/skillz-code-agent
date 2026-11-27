import type { VerificationCheckResult, VerificationReport } from '../../../../shared/agentTypes';
import { PathText } from '../PathText';

const statusLabels = {
  pass: 'Passed',
  fail: 'Failed',
  blocked: 'Blocked',
  unverified: 'Unverified',
  not_run: 'Not run',
  pending: 'Pending',
};

function displayCheck(check: VerificationCheckResult): VerificationCheckResult {
  // Older runs stored missing verdicts as failures or blockers.
  if (check.status === 'fail' && (check.evidence === `The tool returned no usable verification verdict for ${check.id}.`
      || check.evidence === 'The browser result did not establish the required UI states and interactions.')) {
    return { ...check, status: 'unverified' };
  }
  if (check.status === 'blocked' && check.evidence === 'No verdict was recorded.') {
    return { ...check, status: check.attempts ? 'unverified' : 'not_run' };
  }
  return check;
}

export function VerificationChecklist({ report, legacyLog, onProceed, onApproveClose, busy }: {
  report: VerificationReport;
  legacyLog?: string;
  onProceed?: () => void;
  onApproveClose?: () => void;
  busy?: boolean;
}): React.JSX.Element {
  const checks = report.checks.map(displayCheck);
  const passed = checks.filter(check => check.status === 'pass').length;
  const nextSteps = checks.filter(check => check.status !== 'pass' && check.next_step);
  const notes = [report.worker_notes, ...(report.follow_up || [])].filter(Boolean);
  const checkpoints = report.mcp_session?.decisions || [];
  const awaitingManualApproval = Boolean(report.awaiting_manual_approval);
  const legacyDetails = !report.stop_reason && legacyLog;
  const missing = checks.filter(check => check.status === 'unverified').length;
  const stopReason = report.stop_reason || (legacyDetails && missing > 0
    ? `${missing} ${missing === 1 ? 'check returned' : 'checks returned'} no usable verdict.`
    : '');
  return <section className="verification-checklist" aria-label="Required verification checks">
    <header className="verification-checklist-header">
      <strong>Verification</strong>
      <span>{passed} of {checks.length} passed</span>
    </header>
    {stopReason && <p className="verification-stop"><PathText>{stopReason}</PathText></p>}
    <ul>
      {checks.map(check => {
        const stopDetail = check.stop_reason === report.stop_reason ? '' : check.stop_reason;
        const needsRetry = check.status === 'fail' || check.status === 'unverified';
        const attemptLabel = needsRetry && check.mcp_attempts !== undefined && check.mcp_attempt_checkpoint !== undefined
          ? `${check.mcp_attempts} of ${check.mcp_attempt_checkpoint} MCP attempts`
          : needsRetry && !check.mcp_backed && report.attempt_limit
          ? `${check.attempts} of ${report.attempt_limit} attempts`
          : check.attempts === 1 ? '1 attempt' : `${check.attempts} attempts`;
        return <li key={check.id}>
          <details>
            <summary>
              <span className="verification-check-heading">
                <span className="verification-check-status" data-status={check.status}>{statusLabels[check.status]}</span>
                <span className="verification-check-attempts">{check.reused_from?.length ? 'Reused evidence' : attemptLabel}</span>
              </span>
              <span className="verification-check-requirement">{check.requirement}</span>
              {stopDetail && <span className="verification-check-stop-detail">{stopDetail}</span>}
            </summary>
            <div className="verification-check-evidence">
              {Boolean(check.reused_from?.length) && <p><strong>Verified by</strong> {check.reused_from?.join(', ')}</p>}
              {check.evidence && <p><PathText>{check.evidence}</PathText></p>}
              {check.progress_evidence && check.status !== 'pass' && <p><strong>Last progress</strong> <PathText>{check.progress_evidence}</PathText></p>}
              {check.latest_attempt && <p><strong>Latest attempt</strong> <PathText>{check.latest_attempt}</PathText></p>}
            </div>
          </details>
        </li>;
      })}
    </ul>
    {awaitingManualApproval && <section className="verification-manual-approval" aria-label="Manual approval required">
      <strong>Manual approval required</strong>
      <p>Playwright verification has unverified or failed checks. Waiting for manual approval to close the issue.</p>
      {onApproveClose && <div className="decision-buttons">
        <button type="button" className="primary-button" disabled={busy} onClick={onApproveClose}>Approve &amp; close issue</button>
      </div>}
    </section>}
    {nextSteps.length > 0 && <section className="verification-next-steps" aria-label="Verification next steps">
      <strong>Next steps</strong>
      {nextSteps.map(check => <div key={check.id}>
        <span>{check.id.replace(/^check-/, 'Check ')}</span>
        <p><PathText>{check.next_step || ''}</PathText></p>
      </div>)}
      {!awaitingManualApproval && onProceed && <div className="decision-buttons">
        <button type="button" disabled={busy} onClick={onProceed}>Proceed with next steps</button>
      </div>}
    </section>}
    {checkpoints.length > 0 && <details className="verification-notes">
      <summary>Progress checkpoints</summary>
      {checkpoints.map(({ turn, calls, reason, evidence }, index) => <div key={index}>
        <p><strong>Turn {turn} · {calls} calls</strong> {reason}</p>
        {evidence?.map((item, evidenceIndex) => <p key={evidenceIndex}><PathText>{item}</PathText></p>)}
      </div>)}
    </details>}
    {(notes.length > 0 || legacyDetails) && <details className="verification-notes">
      <summary>{legacyDetails ? 'Original run report' : 'Run notes'}</summary>
      {legacyDetails ? <p><PathText>{legacyDetails}</PathText></p> : notes.map((note, index) => <p key={index}><PathText>{note}</PathText></p>)}
    </details>}
  </section>;
}
