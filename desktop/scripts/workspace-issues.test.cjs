const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const Module = require('node:module');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const load = require('./load-ts.cjs');
const { issuesFromLedger, issueManagementView, parseIssueProposals, readWorkspaceIssues, ManagedIssueCard, RepoFactsView, issuesSnapshot, issuesBridge, savedProposals, repoFactsSnapshot, repoFactsMarkdown } = load(() => ({
  ...require('../src/shared/workspaceIssues.ts'), ...require('../src/main/services/workspaceIssues.ts'), ...require('../src/renderer/src/components/AgentIssues.tsx'), ...require('../src/renderer/src/components/RepoFactsPanel.tsx'), ...require('./fixtures/workspace-issues.ts'), ...require('./fixtures/repo-facts.ts'),
}));
function fixture(t) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'skillz issues ë '));
  t.after(()=>{assert.equal(fs.realpathSync(path.dirname(root)),fs.realpathSync(os.tmpdir()));assert.ok(path.basename(root).startsWith('skillz issues ë '));fs.rmSync(root,{recursive:true,force:true,maxRetries:3});});
  return root;
}
test('issue projection includes closed records and history, excludes repository fact scopes and fact bodies',()=>{
  const facts=repoFactsSnapshot('alpha').ledger;
  const issues=issuesFromLedger(facts);
  assert.deepEqual(issues.map(x=>x.id),['issue-042','issue-039','issue-001']);
  assert.equal(issues[0].checkpoints[0].title,'Read the persisted ledger safely');
  assert.equal(issues[0].notes[0],'Discovery identified the persisted schema.');
  assert.equal(issues[0].facts,undefined);
  const many={...facts,issues:Array.from({length:12},(_,i)=>({...facts.issues[2],id:`issue-${i}`}))};
  assert.equal(issuesFromLedger(many).length,12);
});
test('stopped runtime shows saved issues and proposals; running data overrides stale saved status',()=>{
  const saved=issuesSnapshot('alpha');
  assert.equal(issueManagementView(saved).issues.length,3);
  assert.equal(issueManagementView(saved).proposals.length,2);
  const live=issuesBridge(saved);
  live.planner.issue_state.active_issue=null;
  live.planner.issue_state.active_issue_id='';
  live.planner.issue_state.issues[0].status='closed';
  live.planner.worker_state.issue_proposals.proposals=[];
  const view=issueManagementView(saved,live);
  assert.equal(view.activeIssueId,'');
  assert.equal(view.issues.find(x=>x.id==='issue-042').status,'closed');
  assert.equal(view.issues.find(x=>x.id==='issue-042').checkpoints.length,1);
  assert.equal(view.proposals.length,0);
  assert.equal(saved.issues[0].status,'open');
  assert.equal(issueManagementView(undefined).issues.length,0);
});
test('proposal reader validates schema and retains only pending proposals in the management view',()=>{
  const proposals=parseIssueProposals(JSON.stringify({version:1,proposals:[...savedProposals,{...savedProposals[0],proposal_id:'done',status:'accepted'},{...savedProposals[0],proposal_id:'ignored',status:'ignored'}]}));
  assert.equal(proposals.length,4);
  assert.equal(issueManagementView({...issuesSnapshot('alpha'),proposals}).proposals.length,2);
  for(const payload of [{version:2,proposals:[]},{version:1,proposals:[null]},{version:1,proposals:[savedProposals[0],savedProposals[0]]}]) assert.throws(()=>parseIssueProposals(JSON.stringify(payload)));
});
test('saved issue/suggestion reads work without starting an agent and never modify their files',async(t)=>{
  const root=fixture(t);
  assert.equal((await readWorkspaceIssues(root)).status,'missing');
  const ledger=path.join(root,'repo_facts.md'), proposals=path.join(root,'.agent-issue-proposals.json');
  fs.writeFileSync(ledger,repoFactsMarkdown,'utf8');
  const proposalText=JSON.stringify({version:1,proposals:savedProposals});fs.writeFileSync(proposals,proposalText,'utf8');
  const snapshot=await readWorkspaceIssues(root);
  assert.equal(snapshot.issues.length,3);assert.equal(snapshot.proposals.length,2);
  assert.equal(fs.readFileSync(ledger,'utf8'),repoFactsMarkdown);assert.equal(fs.readFileSync(proposals,'utf8'),proposalText);
  fs.writeFileSync(proposals,'{malformed');
  const invalid=await readWorkspaceIssues(root);
  assert.equal(invalid.issues.length,3);assert.ok(invalid.proposalError);
  fs.writeFileSync(ledger,'{malformed');fs.writeFileSync(proposals,proposalText);
  const badLedger=await readWorkspaceIssues(root);
  assert.equal(badLedger.status,'invalid');assert.equal(badLedger.issues.length,0);assert.equal(badLedger.proposals.length,2);
});
test('issue cards keep lifecycle actions outside disclosure; facts display no issue workflow/history',()=>{
  const issue=issuesSnapshot('alpha').issues[0];
  const html=renderToStaticMarkup(React.createElement(ManagedIssueCard,{issue,active:true,expanded:false,busy:false,pending:false,onToggle(){},onAction(){}}));
  assert.match(html,/Close issue-042/);assert.match(html,/Continue issue-042/);
  assert.doesNotMatch(html,/Lifecycle notes|Completed goals/);
  const details=renderToStaticMarkup(React.createElement(ManagedIssueCard,{issue,expanded:true,onToggle(){},onAction(){}}));
  assert.match(details,/Lifecycle notes/);assert.match(details,/Completed goals/);
  const closed=renderToStaticMarkup(React.createElement(ManagedIssueCard,{issue:{...issue,status:'closed'},expanded:false,onToggle(){},onAction(){}}));assert.match(closed,/Reopen issue-042/);
  const facts=renderToStaticMarkup(React.createElement(RepoFactsView,{ledger:repoFactsSnapshot('alpha').ledger,onOpenIssue(){}}));
  assert.match(facts,/facts.offline/);assert.match(facts,/View issue-042 in Issues/);
  assert.doesNotMatch(facts,/Scope metadata|Lifecycle notes|Completed goals|Read the persisted ledger safely|Initial repository setup|Close issue|Reopen issue|Continue issue/);
});
test('issues IPC checks the sender and root; service rejects a read completed after a folder switch',async(t)=>{
  const handlers=new Map();const original=Module._load;
  t.mock.method(Module,'_load',function(request,...rest){if(request==='electron')return {ipcMain:{removeHandler(){},removeAllListeners(){},on(){},handle:(name,listener)=>handlers.set(name,listener)},dialog:{},shell:{}};return original.call(this,request,...rest);});
  const {registerIpc,WorkspaceService}=load(()=>({...require('../src/main/ipc.ts'),...require('../src/main/services/workspace.ts')}));
  const service=new WorkspaceService(()=>{});const root=fixture(t);fs.writeFileSync(path.join(root,'repo_facts.md'),repoFactsMarkdown);
  let current=root;service.requireRoot=()=>current;
  registerIpc({webContents:{id:42}},{workspace:service});
  const handler=handlers.get('workspace:issues');assert.throws(()=>handler({sender:{id:99}},root),/Untrusted/);assert.throws(()=>handler({sender:{id:42}},''));
  await assert.rejects(service.issues(root+'-other'),/Workspace changed/);
  const originalRead=fs.promises.readFile;let began,release;const entered=new Promise(r=>began=r);
  t.mock.method(fs.promises,'readFile',async(...args)=>{began();await new Promise(r=>release=r);return originalRead(...args);});
  const pending=assert.rejects(service.issues(root),/Workspace changed/);await entered;current=root+'-next';release();await pending;
});
