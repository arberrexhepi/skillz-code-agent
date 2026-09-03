const assert = require('node:assert/strict');
const fs = require('node:fs'); const os = require('node:os'); const path = require('node:path');
const { test } = require('node:test'); const load = require('./load-ts.cjs');
const { ArtifactLibraryService, ArtifactSandbox, ArtifactsService, RuntimeSettingsService } = load(() => ({...require('../src/main/services/artifactLibrary.ts'),...require('../src/main/services/artifactSandbox.ts'),...require('../src/main/services/artifacts.ts'),...require('../src/main/services/runtimeSettings.ts')}));
const harness = path.resolve(__dirname, '../..');
function completed(child) { return new Promise((resolve,reject)=>{let out='',err='';child.stdout.setEncoding('utf8');child.stderr.setEncoding('utf8');child.stdout.on('data',s=>out+=s);child.stderr.on('data',s=>err+=s);child.on('error',reject);child.on('close',code=>code===0?resolve(out):reject(new Error(err||out)));child.stdin.end();}); }
test('Docker enforces read grants for direct Node, subprocesses and agent tools; revocation and separate agent backends work', {timeout:240000}, async(t)=>{
  const temp=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'skillz sandbox ë ')));
  const library=new ArtifactLibraryService(path.join(temp,'settings.json'),path.resolve(__dirname,'../artifact-template'),path.join(temp,'contexts'));
  const documents=path.join(temp,'documents'), outside=path.join(temp,'outside'),workspace=path.join(temp,'workspace');
  for(const directory of [documents,outside,workspace])fs.mkdirSync(directory);
  fs.writeFileSync(path.join(documents,'notes ë.txt'),'Document ë'); fs.writeFileSync(path.join(outside,'secret.txt'),'UNAPPROVED');fs.writeFileSync(path.join(workspace,'code.ts'),'Workspace');
  fs.symlinkSync(outside,path.join(documents,'escape'),process.platform==='win32'?'junction':'dir');
  const events=[];
  const service=new ArtifactsService(library,new RuntimeSettingsService(path.join(temp,'runtime.json')),event=>{events.push(event);if(event.type==='agent'&&event.event.type==='stderr')console.log(event.event.message.slice(-1200));},()=>workspace);
  const sandboxes=[];
  t.after(async()=>{await service.dispose();for(const sandbox of sandboxes)await sandbox.stop();assert.equal(fs.realpathSync(path.dirname(temp)),fs.realpathSync(os.tmpdir()));assert.ok(path.basename(temp).startsWith('skillz sandbox ë '));fs.rmSync(temp,{recursive:true,force:true,maxRetries:5});});
  await library.configure(path.join(temp,'library'));
  const record=await library.create({title:'Sandbox',prompt:'Read files',sourceRoot:'',shareFacts:false,shareMemory:false,access:{directories:[{id:'documents',label:'Documents',path:fs.realpathSync(documents)}],allowWorkspaceRead:true}});
  const sandbox=await service.sandbox(record.id);sandboxes.push(sandbox);
  const prepared=await sandbox.prepare(()=>{},harness);
  const script=`const fs=require('fs'),cp=require('child_process');const out={read:fs.readFileSync('/reads/documents/notes ë.txt','utf8'),workspace:fs.readFileSync('/reads/workspace/code.ts','utf8'),dockerSocket:fs.existsSync('/var/run/docker.sock')};
for(const file of ['/reads/documents/notes ë.txt','/reads/workspace/code.ts','/context/new.txt']){try{fs.writeFileSync(file,'BAD');out[file]='WRITABLE';}catch(e){out[file]=e.code;}}
try{out.escape=fs.readFileSync('/reads/documents/escape/secret.txt','utf8');}catch(e){out.escape=e.code;}
try{cp.execFileSync('sh',['-c','echo BAD > /reads/documents/notes.txt'],{stdio:'pipe'});out.shell='WRITABLE';}catch(e){out.shell='denied';}
fs.writeFileSync('/repo/own.txt','own');
out.readTool=JSON.parse(cp.execFileSync('python3',['/opt/skillz/agent_tools.py','read','--root','/repo','--path','/reads/documents/notes ë.txt'],{encoding:'utf8'}));
out.listTool=JSON.parse(cp.execFileSync('python3',['/opt/skillz/agent_tools.py','ls','--root','/repo','--path','/reads/documents'],{encoding:'utf8'}));
out.beta=JSON.parse(cp.execFileSync('python3',['-c',"import json;from pathlib import Path;from discovery.dispatch import execute_discovery_action;print(json.dumps(execute_discovery_action({'type':'read_file','path':'/reads/documents/notes ë.txt'},root=Path('/repo'))))"],{encoding:'utf8',env:{...process.env,PYTHONPATH:'/opt/skillz'}}));
try{cp.execFileSync('python3',['/opt/skillz/agent_tools.py','write','--root','/repo','--path','/reads/documents/notes ë.txt','--content','BAD'],{stdio:'pipe'});out.writeTool='WRITABLE';}catch(e){out.writeTool='denied';}
console.log(JSON.stringify(out));`;
  const result=JSON.parse(await completed(sandbox.spawn(prepared.docker,prepared.args,['node','-e',script])));
  assert.equal(result.read,'Document ë');assert.equal(result.workspace,'Workspace');assert.equal(result.dockerSocket,false);assert.notEqual(result.escape,'UNAPPROVED');assert.equal(result.shell,'denied');assert.equal(result.writeTool,'denied');
  for(const file of ['/reads/documents/notes ë.txt','/reads/workspace/code.ts','/context/new.txt'])assert.equal(result[file],'EROFS');
  assert.equal(result.readTool.ok,true);assert.match(JSON.stringify(result.readTool),/Document ë/);assert.equal(result.beta.ok,true);assert.match(result.beta.content,/Document ë/);assert.equal(result.listTool.ok,true);assert.equal(fs.readFileSync(path.join(record.root,'own.txt'),'utf8'),'own');assert.equal(fs.readFileSync(path.join(documents,'notes ë.txt'),'utf8'),'Document ë');
  console.log('Actual Docker read/write, subprocess, junction escape and structured read checks passed.');
  for(const backendScript of ['main.py','main_v2.py']){
    const agent=await service.agent(record.id);agent.agentRoot=()=>harness;
    const response=await service.startAgent(record.id,{provider:'codex-subscription',model:'gpt-5.4',backendScript});assert.equal(response.ok,true);if(backendScript==='main.py')await agent.stop();
  }
  // This container is absent from service.runtimes/agents, like one orphaned by an app crash.
  const orphan=new ArtifactSandbox(record.root,sandbox.reads,await library.contextDirectory(record.id));sandboxes.push(orphan);const prior=await orphan.prepare(()=>{},harness);
  const orphanProcess=orphan.spawn(prior.docker,prior.args,['node','-e',"console.log('ready');setInterval(()=>{},1000)"]);
  await new Promise((resolve,reject)=>{orphanProcess.stdout.once('data',resolve);orphanProcess.once('error',reject);});
  const orphanExit=new Promise(resolve=>orphanProcess.once('close',resolve));
  await service.saveAccess(record.id,{directories:[],allowWorkspaceRead:false});
  await orphanExit;
  assert.equal(events.filter(event=>event.type==='agent'&&event.event.type==='status').at(-1).event.status,'stopped');
  const revoked=await service.sandbox(record.id);sandboxes.push(revoked);const none=await revoked.prepare(()=>{},harness);
  const after=JSON.parse(await completed(revoked.spawn(none.docker,none.args,['node','-e',"console.log(JSON.stringify({reads:require('fs').existsSync('/reads/documents'),workspace:require('fs').existsSync('/reads/workspace')}))"])));
  assert.deepEqual(after,{reads:false,workspace:false});
});

test('container model requests use the local helper, stream progress, and do not inherit host credentials', {timeout:120000}, async(t)=>{
  const temp=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'skillz model broker ')));
  const library=new ArtifactLibraryService(path.join(temp,'settings.json'),path.resolve(__dirname,'../artifact-template'),path.join(temp,'contexts'));
  await library.configure(path.join(temp,'library'));const record=await library.create({title:'Broker',prompt:'Fixture',sourceRoot:'',shareFacts:false,shareMemory:false});
  const source=path.join(temp,'harness');fs.mkdirSync(source);fs.copyFileSync(path.join(harness,'artifact_agent_entry.py'),path.join(source,'artifact_agent_entry.py'));
  fs.writeFileSync(path.join(source,'main.py'),`import json, os
class BackoffStrategy:
    def __init__(self, **kwargs): self.enabled=False; self.token_limit_k=0
class BaseModelClient:
    def _set_last_metrics(self, value): self.metrics=value
    def get_last_metrics(self): return self.metrics

def main():
    client=create_model_client(provider='fixture',model='fixture')
    events=[]
    client.set_progress_callback(events.append)
    result=client.complete_messages('system',[{'role':'user','content':'hello ë'}])
    print(json.dumps({'broker_result':result,'events':events,'metrics':client.get_last_metrics(),'host_secret':os.environ.get('BROKER_TEST_SECRET')}),flush=True)
    return 0
`);
  fs.writeFileSync(path.join(source,'artifact_model_host.py'),`import json,sys,os
request=json.loads(sys.stdin.read())
if '--check' in sys.argv:
    print(json.dumps({'ready':True}),flush=True)
    raise SystemExit(0)
print(json.dumps({'progress':{'type':'turn.started'}}),flush=True)
print(json.dumps({'text':request['messages'][0]['content'],'metrics':{'host_configured':os.environ.get('BROKER_TEST_SECRET')=='sentinel'}}),flush=True)
`);
  const {ArtifactAgentExecution}=load(()=>require('../src/main/services/artifactAgent.ts'));
  const sandbox=new ArtifactSandbox(record.root,[],await library.contextDirectory(record.id));
  const execution=new ArtifactAgentExecution(async()=>sandbox,()=>{});
  t.after(async()=>{await execution.stop();assert.equal(fs.realpathSync(path.dirname(temp)),fs.realpathSync(os.tmpdir()));assert.ok(path.basename(temp).startsWith('skillz model broker '));fs.rmSync(temp,{recursive:true,force:true,maxRetries:5});});
  const child=await execution.launch({agentRoot:source,scriptName:'main.py',python:{executable:process.platform==='win32'?'py':'python3',args:process.platform==='win32'?['-3']:[]},env:{...process.env,PYTHONIOENCODING:'utf-8',BROKER_TEST_SECRET:'sentinel'},options:{provider:'fixture',model:'fixture'}});
  const result=await new Promise((resolve,reject)=>{let buffer='',errors='';child.stdout.setEncoding('utf8');child.stderr.setEncoding('utf8');child.stderr.on('data',text=>errors+=text);child.stdout.on('data',text=>{buffer+=text;let newline;while((newline=buffer.indexOf('\n'))>=0){const payload=JSON.parse(buffer.slice(0,newline));buffer=buffer.slice(newline+1);if(!execution.message(payload)&&payload.broker_result)resolve(payload);}});child.on('error',reject);child.on('close',code=>{if(code)reject(new Error(errors));});});
  assert.equal(result.broker_result,'hello ë');assert.equal(result.host_secret,null);assert.equal(result.metrics.host_configured,true);assert.deepEqual(result.events,[{type:'turn.started'}]);
});
