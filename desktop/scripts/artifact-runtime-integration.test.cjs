const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const load = require('./load-ts.cjs');
const { ArtifactLibraryService, ArtifactsService, ArtifactSandbox, AgentService, RuntimeSettingsService, artifactDependencyVolume, artifactRuntimeImage, dockerCommand, command } = load(() => ({
  ...require('../src/main/services/artifactLibrary.ts'), ...require('../src/main/services/artifacts.ts'),
  ...require('../src/main/services/artifactSandbox.ts'), ...require('../src/main/services/agent.ts'),
  ...require('../src/main/services/runtimeSettings.ts'), ...require('../src/main/services/artifactProcess.ts'),
}));
const harness = path.resolve(__dirname, '../..');
const selection = { provider: 'gemini', model: 'fixture-model', backendScript: 'main.py' };

// A deterministic NDJSON backend exercises the real Docker execution, host model
// broker, desktop recording, and Express gateway without paid provider calls.
async function fixtureHarness(root) {
  const source = path.join(root, 'harness'); await fs.mkdir(source);
  for (const name of ['artifact_agent_entry.py', 'artifact_app_agent.py', 'main_v2.py']) await fs.copyFile(path.join(harness, name), path.join(source, name));
  await fs.writeFile(path.join(source, 'agent_tools.py'), '');
  await fs.writeFile(path.join(source, 'main.py'), `class BackoffStrategy:
    def __init__(self, **kwargs): self.enabled=False; self.token_limit_k=0
class BaseModelClient:
    def _set_last_metrics(self, value): self.metrics=value
    def get_last_metrics(self): return getattr(self, 'metrics', {})
`);
  await fs.writeFile(path.join(source, 'artifact_model_host.py'), `import json,sys
request=json.load(sys.stdin)
if '--capabilities' in sys.argv: print(json.dumps({'sdkReady':True,'keyReady':True,'label':'Fixture'}))
elif '--check' in sys.argv: print(json.dumps({'ready':True}))
else: print(json.dumps({'text':'Fixture model response','metrics':{}}))
`);
  await fs.writeFile(path.join(source, 'live_test_loop.py'), `import errno,json,sys,os,subprocess
from pathlib import Path
from main import create_model_client
def main():
    provider=sys.argv[sys.argv.index('--provider')+1]; model=sys.argv[sys.argv.index('--model')+1]
    client=create_model_client(provider=provider,model=model)
    plan={'summary':'Read the shared evidence','goals':[]}
    for line in sys.stdin:
        request=json.loads(line); kind=request.get('type'); status='idle'; message='Ready'
        if kind=='submit': status='awaiting_plan_approval'; message='Review the plan'
        if kind=='planner_action':
            assert request['action']=='approve_plan'
            print(json.dumps({'type':'progress','action_type':'model_call_start','step':1}),flush=True)
            model_output=client.complete('fixture','Read the granted evidence')
            print(json.dumps({'type':'progress','action_type':'model_call_finish','ok':True,'step':1}),flush=True)
            root=Path(sys.argv[sys.argv.index('--root')+1]); file=root/'evidence.txt'; content=file.read_text(); denied=False
            grant=json.loads(os.environ['SKILLZ_READ_ROOTS'])[0]
            git_result=subprocess.run(['git','ls-files'],cwd=root,capture_output=True,text=True)
            scope={'cwd':os.getcwd(),'root':str(root),'access':grant['access'],'git_files':git_result.stdout.strip(),'git_ok':git_result.returncode==0,'owner_visible':(root/'observatory-only.txt').exists(),'other_mounts':[p.name for p in Path('/reads').iterdir() if p.name!=grant['id']]}
            try: file.write_text('Changed by explicit fixture approval')
            except OSError as error:
                if error.errno != errno.EROFS: raise
                denied=True
            print(json.dumps({'type':'progress','action_type':'run_check','ok':denied==(grant['access']=='read'),'scope':scope,'summary':content,'host_secret':os.environ.get('OBSERVATORY_TEST_SECRET'),'step':2}),flush=True)
            status='completed'; message=model_output+'; '+content
        planner={'status':status}
        if status=='awaiting_plan_approval': planner.update(pending_plan=plan,suggested_next_actions=[{'type':'approve_plan'}])
        print(json.dumps({'id':request['id'],'ok':True,'message':message,'state':{'planner':planner,'transcript':[]}}),flush=True)
    return 0
`);
  return source;
}
async function json(url, body) {
  const response = await fetch(url, { method: body === undefined ? 'GET' : 'POST', headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(30000) });
  const value = await response.json(); assert.equal(response.ok, true, JSON.stringify(value)); return value;
}
async function idle(url) {
  for (let attempt = 0; attempt < 300; attempt++) {
    const status = await json(url + '/runtime/v1/runtime');
    if (!status.busy) return status;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('Container recording did not settle.');
}
test('installed Observatory records an isolated Docker run through explicit approval, replays after restart, and revokes access', { timeout: 420000 }, async t => {
  const root = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'skillz-observatory-docker-')));
  const source = await fixtureHarness(root);
  const library = new ArtifactLibraryService(path.join(root, 'library.json'), path.resolve(__dirname, '../artifact-template'), path.join(root, 'contexts'));
  const settings = new RuntimeSettingsService(path.join(root, 'runtime.json'));
  const events = [];
  const service = new ArtifactsService(library, settings, event => { events.push(event); if (event.type === 'runtime' && event.runtime.error) console.error(event.runtime.error, event.runtime.logs.slice(-2000)); });
  let record;
  t.after(async () => {
    await service.dispose();
    const docker = await dockerCommand();
    if (record) await command(docker, ['volume', 'rm', artifactDependencyVolume(record.root)], root).catch(() => {});
    await command(docker, ['image', 'rm', await artifactRuntimeImage(source)], root).catch(() => {});
    await fs.rm(root, { recursive: true, force: true });
  });
  const prepare = ArtifactSandbox.prototype.prepare;
  t.mock.method(ArtifactSandbox.prototype, 'prepare', function (log) { return prepare.call(this, log, source); });
  t.mock.method(AgentService.prototype, 'agentRoot', () => source);
  t.mock.method(service.capabilities, 'hostContext', async context => context);
  service.capabilities.source = () => source;
  t.mock.method(service.capabilities, 'modelRequest', async () => ({ text: 'App chatbot broker response' }));
  const oldSecret = process.env.OBSERVATORY_TEST_SECRET;
  process.env.OBSERVATORY_TEST_SECRET = 'host-only-sentinel';
  t.after(() => { if (oldSecret === undefined) delete process.env.OBSERVATORY_TEST_SECRET; else process.env.OBSERVATORY_TEST_SECRET = oldSecret; });
  await library.configure(path.join(root, 'library'));
  const shared = path.join(root, 'shared'); await fs.mkdir(shared); await fs.writeFile(path.join(shared, 'evidence.txt'), 'Read-only evidence from the shared repository');
  await command('git', ['init'], shared); await command('git', ['add', 'evidence.txt'], shared); await command('git', ['-c','user.name=Fixture','-c','user.email=fixture@example.test','commit','-m','Fixture evidence'], shared);
  const editableMain=path.join(root,'editable-main'), editable=path.join(root,'editable-worktree'); await fs.mkdir(editableMain);
  await command('git',['init'],editableMain); await fs.writeFile(path.join(editableMain,'evidence.txt'),'Editable worktree evidence');
  await command('git',['add','evidence.txt'],editableMain); await command('git',['-c','user.name=Fixture','-c','user.email=fixture@example.test','commit','-m','Fixture'],editableMain);
  await command('git',['worktree','add','--detach',editable],editableMain);
  const docs=path.join(editableMain,'docs'); await fs.mkdir(docs); await fs.writeFile(path.join(docs,'evidence.txt'),'Read-only folder evidence');
  const access = { directories: [{ id: 'project', label: 'Project', path: shared, access: 'read' }, { id: 'editable', label: 'Editable worktree', path: editable, access: 'write' }, { id: 'docs', label: 'Docs folder', path: docs, access: 'read' }], allowWorkspaceRead: false, allowAgentRuntime: true };
  record = await library.installPrebuilt('beta-observatory', access, selection);
  await fs.writeFile(path.join(record.root,'observatory-only.txt'),'Artifact source must stay outside the selected shared worker');
  await library.saveApp(record.id, { version: 1, chatbot: { enabled: true }, database: { enabled: false } });
  assert.equal(await fs.readFile(path.join(record.root, 'server/runtime.ts'), 'utf8'), await fs.readFile(path.resolve(__dirname, '../prebuilt-artifacts/beta-observatory/server/runtime.ts'), 'utf8'));
  console.log('Starting installed Observatory through the current desktop service.');
  let preview = await service.start(record.id);
  const runtime = await json(preview.url + '/runtime/v1/runtime');
  assert.deepEqual(runtime.repositories.map(item=>item.id),['repo','project','editable','docs']);
  assert.equal(runtime.available, true); assert.equal(runtime.ready, true); assert.equal(runtime.setupAvailable, true); assert.equal(runtime.selection.backendScript, 'main_v2.py');
  await json(preview.url + '/runtime/v1/configure', {});
  assert.ok(events.some(event => event.type === 'runtime-setup' && event.id === record.id));
  const crossOrigin = await fetch(preview.url + '/runtime/v1/configure', { method: 'POST', headers: { 'content-type': 'application/json', origin: 'https://example.com' }, body: '{}' });
  assert.equal(crossOrigin.status, 403);
  assert.doesNotMatch(JSON.stringify(runtime), /SKILLZ_RUNTIME_TOKEN|host-only-sentinel/);
  const commands = await json(preview.url + '/_skillz/commands'); assert.ok(Array.isArray(commands.commands));
  const chat = await json(preview.url + '/_skillz/agent/chat', { message: 'Check chatbot integration' }); assert.equal(chat.message, 'App chatbot broker response');
  const { id } = await json(preview.url + '/runtime/v1/runs', { text: 'Read the shared evidence after approval', repositoryId: 'project' });
  await idle(preview.url);
  let run = await json(preview.url + '/runtime/v1/runs/' + id); assert.equal(run.status, 'awaiting-input');
  assert.equal(run.events.some(event => event.data.action_type === 'run_check'), false, 'Execution must await explicit approval.');
  await json(preview.url + '/runtime/v1/runs/' + id + '/continue', { action: 'approve_plan' }); await idle(preview.url);
  run = await json(preview.url + '/runtime/v1/runs/' + id); assert.equal(run.status, 'completed', JSON.stringify(run));
  const check = run.events.find(event => event.data.action_type === 'run_check'); assert.equal(check.data.ok, true); assert.equal(check.data.host_secret, null);
  assert.deepEqual(run.repository,{id:'project',label:'Project',access:'read'});
  assert.deepEqual(check.data.scope,{cwd:'/repo',root:'/repo',access:'read',git_files:'evidence.txt',git_ok:true,owner_visible:false,other_mounts:[]});
  assert.match(run.message, /Fixture model response/); assert.match(run.message, /Read-only evidence/);
  assert.equal(await fs.readFile(path.join(shared, 'evidence.txt'), 'utf8'), 'Read-only evidence from the shared repository');
  const history = path.join(settings.artifactSetupDirectory(), 'run-history', record.id, id + '.json');
  await service.stop(record.id); assert.equal(JSON.parse(await fs.readFile(history, 'utf8')).status, 'completed');
  preview = await service.start(record.id);
  assert.deepEqual((await json(preview.url + '/runtime/v1/runs/' + id)).events, run.events);
  for (const repositoryId of ['editable','docs']) {
    const next=await json(preview.url+'/runtime/v1/runs',{text:'Inspect the selected repository; the fixture edit is explicitly permitted for the editable worktree',repositoryId});
    await idle(preview.url); await json(preview.url+'/runtime/v1/runs/'+next.id+'/continue',{action:'approve_plan'}); await idle(preview.url);
    const scoped=await json(preview.url+'/runtime/v1/runs/'+next.id); assert.equal(scoped.status,'completed');
    assert.equal(scoped.repository.id,repositoryId); const observed=scoped.events.find(event=>event.data.action_type==='run_check'); assert.equal(observed.data.ok,true);
    assert.equal(observed.data.scope.owner_visible,false); assert.deepEqual(observed.data.scope.other_mounts,[]);
    if(repositoryId==='editable') { assert.equal(observed.data.scope.access,'write'); assert.equal(observed.data.scope.git_files,'evidence.txt'); assert.equal(observed.data.scope.git_ok,true); }
    else { assert.equal(observed.data.scope.access,'read'); assert.equal(observed.data.scope.git_ok,false,'A subfolder grant must not expose parent Git metadata.'); }
  }
  assert.equal(await fs.readFile(path.join(editable,'evidence.txt'),'utf8'),'Changed by explicit fixture approval');
  assert.equal(await fs.readFile(path.join(editableMain,'evidence.txt'),'utf8'),'Editable worktree evidence');
  assert.equal(await fs.readFile(path.join(docs,'evidence.txt'),'utf8'),'Read-only folder evidence');
  assert.equal(await fs.readFile(path.join(shared,'evidence.txt'),'utf8'),'Read-only evidence from the shared repository');
  const smoke=new ArtifactSandbox(record.root,[access.directories[0]],await library.contextDirectory(record.id),undefined,undefined,access.directories[0]);
  const trusted=await prepare.call(smoke,()=>{},harness);
  const child=smoke.spawn(trusted.docker,trusted.args,['python3','-u','/opt/skillz/artifact_agent_entry.py','main_v2.py','--provider','fixture','--model','fixture','--root','/repo','--tools','/opt/skillz/agent_tools.py','--extension-bridge'],['-i']);
  try {
    const initialized=await new Promise((resolve,reject)=>{let output='',errors='';child.stdout.setEncoding('utf8');child.stderr.setEncoding('utf8');child.stdout.on('data',data=>output+=data);child.stderr.on('data',data=>errors+=data);child.on('error',reject);child.on('close',code=>code===0?resolve(output):reject(new Error(errors||output)));child.stdin.end(JSON.stringify({id:'read-only-initialize',type:'initialize'})+'\n');});
    const messages=initialized.trim().split('\n').map(line=>JSON.parse(line)); assert.ok(messages.some(message=>message.id==='read-only-initialize'&&message.ok===true),'The real beta backend must initialize in a read-only repository without any model call.');
  } finally { await smoke.stop(); }
  const paused = await json(preview.url + '/runtime/v1/runs', { text: 'Pause this run' }); await idle(preview.url);
  await service.saveAccess(record.id, { ...access, allowAgentRuntime: false });
  assert.equal(JSON.parse(await fs.readFile(path.join(path.dirname(history), paused.id + '.json'), 'utf8')).status, 'interrupted');
  preview = await service.start(record.id);
  assert.equal((await json(preview.url + '/runtime/v1/runtime')).available, false);
  assert.equal((await fetch(preview.url + '/runtime/v1/runs', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ text: 'Denied' }) })).status, 503);
  console.log('Selected read-only repository, editable Git worktree, subfolder boundary, real beta initialization, both brokers, replay, and revocation verified.');
});
