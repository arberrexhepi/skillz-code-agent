import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createIssue, newLedger, parseLedgerDocument, serializeLedger, transitionIssue } from '../src/issues';

test('issue lifecycle preserves unknown ledger data and surrounding markdown', () => {
  const source = '# Repository Facts\n\nIntro.\n\n```json\n' + JSON.stringify({ schema_version: 2, active_issue_id: 'issue-002', custom: { keep: true }, issues: [
    { issue_id: 'global-architecture', status: 'closed', facts: [{ key: 'keep' }] },
    { issue_id: 'issue-002', status: 'open', request_summary: 'Existing', facts: [] },
  ], migration: {} }, null, 2) + '\n```\n\nFooter.\n';
  const document = parseLedgerDocument(source);
  const id = createIssue(document.ledger, ' New issue ', 'Plan it', '2026-09-03T10:00:00Z');
  assert.equal(id, 'issue-003'); assert.equal(document.ledger.active_issue_id, id);
  transitionIssue(document.ledger, id, 'close', '2026-09-03T11:00:00Z');
  assert.equal(document.ledger.active_issue_id, ''); assert.equal(document.ledger.issues.at(-1)?.closed_at, '2026-09-03T11:00:00Z');
  transitionIssue(document.ledger, id, 'reopen'); assert.equal(document.ledger.active_issue_id, id); assert.equal(document.ledger.issues.at(-1)?.reopen_count, 1);
  transitionIssue(document.ledger, 'issue-002', 'activate'); assert.equal(document.ledger.active_issue_id, 'issue-002');
  const saved = serializeLedger(document, document.ledger); assert.match(saved, /^# Repository Facts/); assert.match(saved, /Footer\./);
  const reparsed = parseLedgerDocument(saved); assert.deepEqual(reparsed.ledger.custom, { keep: true }); assert.equal(reparsed.ledger.issues[0].facts[0].key, 'keep');
});

test('new repositories receive a valid empty schema and invalid ledgers are rejected', () => {
  const document = parseLedgerDocument(newLedger()); assert.equal(document.ledger.schema_version, 2); assert.deepEqual(document.ledger.issues, []);
  assert.throws(() => parseLedgerDocument('not json'), /fenced JSON/);
  assert.throws(() => parseLedgerDocument('```json\n{"schema_version":1,"issues":[]}\n```'), /schema-version-2/);
  assert.throws(() => createIssue(document.ledger, '  ', ''), /required/);
});
