const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const load = require('./load-ts.cjs');
const { ArtifactLibraryService, artifactApisSchema, git } = load(() => ({ ...require('../src/main/services/artifactLibrary.ts'), ...require('../src/shared/artifacts.ts'), ...require('../src/main/services/artifactProcess.ts') }));
function fixture(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'skillz artifacts ë ')));
  // Async removal avoids the bundled runtime's Windows rmSync Unicode-path failure.
  t.after(async () => { assert.equal(fs.realpathSync(path.dirname(root)), fs.realpathSync(os.tmpdir())); assert.ok(path.basename(root).startsWith('skillz artifacts ë ')); await fs.promises.rm(root, { recursive: true, force: true, maxRetries: 5 }); });
  const library = new ArtifactLibraryService(path.join(root, 'settings.json'), path.resolve(__dirname, '../artifact-template'), path.join(root, 'contexts'));
  return { root, library };
}
test('artifact library persists its folder, creates independent Git submodules, shares context and excludes local data', async (t) => {
  const {root, library} = fixture(t);
  assert.deepEqual(await library.library(), {root:'', artifacts:[]});
  const folder=path.join(root,'library'), source=path.join(root,'source');fs.mkdirSync(source);fs.writeFileSync(path.join(source,'repo_facts.md'),'Facts ë');fs.writeFileSync(path.join(source,'memory_observability.md'),'Memory');
  await library.configure(folder);
  const runtime={provider:'gemini',model:'gemini-3-flash-preview',backendScript:'main_v2.py'};
  const artifact=await library.create({title:'Schema kërkesë',prompt:'Render our database schema as tables.',sourceRoot:source,shareFacts:true,shareMemory:true,runtime});
  const context=await library.contextDirectory(artifact.id);
  assert.deepEqual((await library.find(artifact.id)).runtime,runtime);
  assert.equal((await library.library()).artifacts[0].id,artifact.id);
  assert.equal(await git(folder,'rev-parse','--show-toplevel'),folder.replaceAll('\\','/'));
  assert.match(await git(folder,'ls-files','--stage',artifact.id),/^160000 /);
  assert.equal(await git(artifact.root,'rev-parse','--show-toplevel'),artifact.root.replaceAll('\\','/'));
  assert.notEqual(await git(artifact.root,'rev-parse','HEAD'),await git(folder,'rev-parse','HEAD'));
  assert.equal(await git(folder,'status','--porcelain'),'');
  assert.equal(fs.readFileSync(path.join(context,'repo_facts.md'),'utf8'),'Facts ë');
  assert.equal(fs.readFileSync(path.join(context,'memory_observability.md'),'utf8'),'Memory');
  fs.writeFileSync(path.join(source,'repo_facts.md'),'Updated facts');await library.syncContext(artifact);
  assert.equal(fs.readFileSync(path.join(context,'repo_facts.md'),'utf8'),'Updated facts');
  const tracked=await git(artifact.root,'ls-files');assert.doesNotMatch(tracked,/\.context|artifact-local/);assert.match(tracked,/src\/App.tsx/);assert.match(tracked,/server\/gateway.ts/);
  const again=new ArtifactLibraryService(path.join(root,'settings.json'),'unused',path.join(root,'contexts'));
  assert.equal((await again.find(artifact.id)).sourceRoot,source);
  await assert.rejects(library.find('../source'));
  await assert.rejects(library.find('absent'));
});
test('creation is serialized and never overwrites an existing folder or unrelated Git index entries',async(t)=>{
  const {root,library}=fixture(t);const folder=path.join(root,'library');await library.configure(folder);
  fs.writeFileSync(path.join(folder,'user-notes.txt'),'Keep me');await git(folder,'add','user-notes.txt');
  const create=()=>library.create({title:'Repeated name',prompt:'Build a tool',sourceRoot:'',shareFacts:false,shareMemory:false});
  const [one,two]=await Promise.all([create(),create()]);assert.notEqual(one.id,two.id);assert.equal((await library.library()).artifacts.length,2);
  assert.match(await git(folder,'status','--porcelain'),/A  user-notes.txt/);
  const unrelated=path.join(root,'unrelated');fs.mkdirSync(unrelated);fs.writeFileSync(path.join(unrelated,'keep.txt'),'untouched');await assert.rejects(library.configure(unrelated),/empty folder/);assert.equal((await library.library()).root,folder);
});
test('creation reuses an empty destination but preserves existing files and rejects directory links', async(t)=>{
  const {root,library}=fixture(t);const folder=path.join(root,'library');await library.configure(folder);
  t.mock.method(require('node:crypto'),'randomUUID',()=> '12345678-1234-4234-8234-123456789abc');
  const create=(title)=>library.create({title,prompt:'Build a tool',sourceRoot:'',shareFacts:false,shareMemory:false});
  const empty=path.join(folder,'empty-12345678');fs.mkdirSync(empty);
  const artifact=await create('Empty');assert.equal(artifact.root,empty);
  assert.ok(fs.existsSync(path.join(empty,'src','App.tsx')));assert.ok(fs.existsSync(path.join(empty,'.artifact','apis.json')));
  const occupied=path.join(folder,'occupied-12345678');fs.mkdirSync(occupied);fs.writeFileSync(path.join(occupied,'notes.txt'),'Keep me');
  await assert.rejects(create('Occupied'),/must be an empty directory/);assert.deepEqual(fs.readdirSync(occupied),['notes.txt']);assert.equal(fs.readFileSync(path.join(occupied,'notes.txt'),'utf8'),'Keep me');
  const file=path.join(folder,'file-12345678');fs.writeFileSync(file,'Existing file');
  await assert.rejects(create('File'),/must be an empty directory/);assert.equal(fs.readFileSync(file,'utf8'),'Existing file');
  const outside=path.join(root,'outside');fs.mkdirSync(outside);const link=path.join(folder,'linked-12345678');
  fs.symlinkSync(outside,link,process.platform==='win32'?'junction':'dir');
  await assert.rejects(create('Linked'),/must be an empty directory/);assert.deepEqual(fs.readdirSync(outside),[]);
  assert.equal((await library.library()).artifacts.length,1);
});

test('connection configuration supports named HTTP/WebSocket shapes, persists edits and rejects invalid definitions',async(t)=>{
  const {root,library}=fixture(t);await library.configure(path.join(root,'library'));const artifact=await library.create({title:'API test',prompt:'Explore',sourceRoot:'',shareFacts:false,shareMemory:false});
  const config=artifactApisSchema.parse({version:1,apis:[{id:'schema',transport:'http',url:'https://example.com/schema',method:'GET',requestSchema:{type:'object'},responseSchema:{type:'array'},headerEnv:{Authorization:'SCHEMA_TOKEN'}},{id:'events',transport:'websocket',url:'wss://example.com/events',requestSchema:{type:'object'},responseSchema:{type:'object'}}]});
  await library.saveApis(artifact.id,config);assert.deepEqual(await library.apis(artifact.id),config);
  for (const apis of [[{...config.apis[0],url:'file:///secret'}],[{...config.apis[0],url:'https://user:password@example.com'}],[config.apis[0],config.apis[0]],[{...config.apis[0],transport:'websocket'}],[{...config.apis[0],id:'../escape'}]])assert.throws(()=>artifactApisSchema.parse({version:1,apis}));
  await assert.rejects(library.saveApis(artifact.id,{version:1,apis:[{...config.apis[0],id:'../escape'}]}));assert.deepEqual(await library.apis(artifact.id),config);
});

test('context snapshots refresh after atomic replacement and do not require symlinks', async(t)=>{
  const {root,library}=fixture(t);await library.configure(path.join(root,'library'));const source=path.join(root,'source');fs.mkdirSync(source);fs.writeFileSync(path.join(source,'repo_facts.md'),'First');
  const original=fs.promises.symlink;t.mock.method(fs.promises,'symlink',async(target,destination,type)=>{if(type==='file')throw Object.assign(new Error('No symlink privilege'),{code:'EPERM'});return original(target,destination,type);});
  const artifact=await library.create({title:'Windows context',prompt:'Read facts',sourceRoot:source,shareFacts:true,shareMemory:true});assert.equal(artifact.contextMode,'snapshot');const context=await library.contextDirectory(artifact.id);assert.equal(fs.lstatSync(path.join(artifact.root,'.context')).isSymbolicLink(),false);
  fs.writeFileSync(path.join(source,'new.md'),'Updated ë');fs.renameSync(path.join(source,'new.md'),path.join(source,'repo_facts.md'));fs.writeFileSync(path.join(source,'memory_observability.md'),'New memory');await library.syncContext(artifact);
  assert.equal(fs.readFileSync(path.join(context,'repo_facts.md'),'utf8'),'Updated ë');assert.equal(fs.readFileSync(path.join(context,'memory_observability.md'),'utf8'),'New memory');fs.unlinkSync(path.join(source,'repo_facts.md'));await library.syncContext(artifact);assert.equal(fs.existsSync(path.join(context,'repo_facts.md')),false);
});
test('artifact agents share no processes, roots, conversations, or observability destinations',async(t)=>{
  const {root,library}=fixture(t);await library.configure(path.join(root,'library'));
  const one=await library.create({title:'First agent',prompt:'Build one',sourceRoot:'',shareFacts:false,shareMemory:false});const two=await library.create({title:'Second agent',prompt:'Build two',sourceRoot:'',shareFacts:false,shareMemory:false});
  const {ArtifactsService,RuntimeSettingsService}=load(()=>({...require('../src/main/services/artifacts.ts'),...require('../src/main/services/runtimeSettings.ts')}));const events=[];const service=new ArtifactsService(library,new RuntimeSettingsService(path.join(root,'runtime.json')),event=>events.push(event));
  const harness=path.join(root,'harness');fs.mkdirSync(harness);fs.writeFileSync(path.join(harness,'agent_tools.py'),'');fs.writeFileSync(path.join(harness,'main.py'),`import json, sys, os
root = sys.argv[sys.argv.index('--root') + 1]
for line in sys.stdin:
    request=json.loads(line)
    print(json.dumps({'id':request['id'],'ok':True,'root':root,'memory':os.environ.get('SKILLZ_OBSERVABILITY_PATH'),'text':request.get('text',''),'state':{'planner':{},'transcript':[]}}),flush=True)
`);
  try {
    const cancelled=service.start(one.id);await service.stop(one.id);await assert.rejects(cancelled,/cancelled/);
    const [first,alsoFirst,second]=await Promise.all([service.agent(one.id),service.agent(one.id),service.agent(two.id)]);assert.equal(first,alsoFirst);assert.notEqual(first,second);first.agentRoot=()=>harness;second.agentRoot=()=>harness;first.execution=undefined;second.execution=undefined;
    const [a,b]=await Promise.all([service.startAgent(one.id,{provider:'fixture',model:'fixture'}),service.startAgent(two.id,{provider:'fixture',model:'fixture'})]);assert.equal(a.root,one.root);assert.equal(b.root,two.root);assert.notEqual(a.memory,b.memory);
    const result=await service.submit(one.id,'Visualize tables ë');assert.match(result.text,/Visualize tables ë/);assert.match(result.text,/Shared source context is read-only/);assert.equal(result.root,one.root);
    await first.stop();assert.equal((await service.submit(two.id,'Still running')).root,two.root);assert.ok(events.some(event=>event.type==='agent'&&event.id===one.id));assert.ok(events.some(event=>event.type==='agent'&&event.id===two.id));
  } finally {await service.dispose();}
});

test('read grants are opt-in, canonical, persisted outside the artifact and cannot be changed from artifact metadata', async(t)=>{
  const {root,library}=fixture(t);await library.configure(path.join(root,'library'));
  const documents=path.join(root,'Documents ë');fs.mkdirSync(documents);
  const record=await library.create({title:'Reads',prompt:'Read documents',sourceRoot:'',shareFacts:false,shareMemory:false});
  assert.deepEqual(await library.access(record.id),{directories:[],allowWorkspaceRead:false});
  const access={directories:[{id:'documents',label:'Documents',path:fs.realpathSync(documents),access:'read'}],allowWorkspaceRead:true};
  await library.saveAccess(record.id,access);assert.deepEqual(await library.access(record.id),access);
  fs.writeFileSync(path.join(record.root,'.artifact-local.json'),JSON.stringify({sourceRoot:root,shareFacts:true,access:{directories:[{id:'secret',path:root}]}}));
  assert.deepEqual(await library.access(record.id),access);assert.equal((await library.find(record.id)).shareFacts,false);
  await assert.rejects(library.saveAccess(record.id,{directories:[{id:'repo',label:'Bad',path:documents}],allowWorkspaceRead:false}));
  await library.saveAccess(record.id,{directories:[],allowWorkspaceRead:false});assert.deepEqual(await library.access(record.id),{directories:[],allowWorkspaceRead:false});
});

test('Finder metadata does not prevent creating a library or artifact and stays outside Git', async t => {
  const { root, library } = fixture(t);
  const folder = path.join(root, 'Finder library');
  fs.mkdirSync(folder);
  fs.writeFileSync(path.join(folder, '.DS_Store'), 'Finder metadata');
  await library.configure(folder);
  assert.equal(fs.readFileSync(path.join(folder, '.DS_Store'), 'utf8'), 'Finder metadata');
  assert.equal(await git(folder, 'status', '--porcelain'), '');
  t.mock.method(require('node:crypto'), 'randomUUID', () => '12345678-1234-4234-8234-123456789abc');
  const destination = path.join(folder, 'finder-12345678');
  fs.mkdirSync(destination); fs.writeFileSync(path.join(destination, '.DS_Store'), 'Keep Finder metadata');
  const record = await library.create({ title: 'Finder', prompt: 'Build a tool', sourceRoot: '', shareFacts: false, shareMemory: false });
  assert.equal(fs.readFileSync(path.join(destination, '.DS_Store'), 'utf8'), 'Keep Finder metadata');
  assert.equal(await git(record.root, 'status', '--porcelain'), '');
  assert.doesNotMatch(await git(record.root, 'ls-files'), /\.DS_Store/);
  const occupied = path.join(root, 'Not empty'); fs.mkdirSync(occupied); fs.mkdirSync(path.join(occupied, '.DS_Store'));
  await assert.rejects(library.configure(occupied), /empty folder/);
});


test('prebuilt issue manager is optional, installs as an independent artifact, and requires an explicit write grant', async(t) => {
  const {root,library}=fixture(t);await library.configure(path.join(root,'library'));
  const repository=path.join(root,'managed repo');fs.mkdirSync(repository);fs.writeFileSync(path.join(repository,'repo_facts.md'),'# Facts\n');
  const preset=(await library.prebuilts()).find(item=>item.id==='repo-issue-manager');assert.ok(preset);assert.equal(preset.requiresWriteAccess,true);
  const readonly={directories:[{id:'managed',label:'Managed repo',path:repository,access:'read'}],allowWorkspaceRead:false};
  await assert.rejects(library.installPrebuilt(preset.id,readonly),/Allow changes/);
  const writable={...readonly,directories:[{...readonly.directories[0],access:'write'}]};
  const artifact=await library.installPrebuilt(preset.id,writable);
  assert.match(fs.readFileSync(path.join(artifact.root,'src','App.tsx'),'utf8'),/Repository issue manager/);
  assert.deepEqual(await library.access(artifact.id),writable);
  assert.match(await git(path.join(root,'library'),'ls-files','--stage',artifact.id),/^160000 /);
  assert.equal(await git(path.join(root,'library'),'status','--porcelain'),'');
});

test('prebuilt Server Manager requires Process Proxy and installs from its dedicated source', async(t) => {
  const {root,library}=fixture(t);await library.configure(path.join(root,'library'));
  const repository=path.join(root,'managed server');fs.mkdirSync(repository);fs.writeFileSync(path.join(repository,'package.json'),JSON.stringify({scripts:{dev:'vite',test:'vitest'}}));
  const preset=(await library.prebuilts()).find(item=>item.id==='server-manager');assert.ok(preset);assert.equal(preset.requiresProcessProxy,true);
  const readonly={directories:[{id:'managed',label:'Managed server',path:repository,access:'read'}],allowWorkspaceRead:false};
  await assert.rejects(library.installPrebuilt(preset.id,readonly),/Allow Process Proxy/);
  const executable={...readonly,directories:[{...readonly.directories[0],allowProcessProxy:true,processProxyAllowlist:['dev','test']}]};
  const artifact=await library.installPrebuilt(preset.id,executable);
  assert.match(fs.readFileSync(path.join(artifact.root,'src','App.tsx'),'utf8'),/Server Manager/);
  assert.deepEqual(await library.access(artifact.id),executable);
  assert.match(await git(path.join(root,'library'),'ls-files','--stage',artifact.id),/^160000 /);
});
