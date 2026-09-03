const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const load = require('./load-ts.cjs');
const { ArtifactCapabilitiesService, processTools, pythonTools, sandboxTools } = load(() => ({
  ...require('../src/main/services/artifactCapabilities.ts'), processTools: require('../src/main/services/artifactProcess.ts'), pythonTools: require('../src/main/services/python.ts'), sandboxTools: require('../src/main/services/artifactSandbox.ts'),
}));
const selection = { provider: 'gemini', model: 'gemini-3-flash-preview' };
function fixture(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'skillz capabilities ')));
  t.after(async()=>{assert.equal(fs.realpathSync(path.dirname(root)),fs.realpathSync(os.tmpdir()));assert.ok(path.basename(root).startsWith('skillz capabilities '));await fs.promises.rm(root,{recursive:true,force:true,maxRetries:5});});
  const state={sdk:false,browser:false,image:false,docker:true}, calls=[], events=[];
  const storage={isEncryptionAvailable:()=>true,getSelectedStorageBackend:()=> 'test',encryptString:(value)=>Buffer.from(Buffer.from(value).toString('base64')),decryptString:(value)=>Buffer.from(value.toString(),'base64').toString()};
  const preview={browserReady:async()=>state.browser,installBrowser:async(log)=>{calls.push(['browser']);state.browser=true;log('Browser ready.');}};
  const tools={...processTools,...pythonTools,...sandboxTools};
  const service=new ArtifactCapabilitiesService(path.join(root,'settings'),preview,progress=>events.push(progress),()=>root,storage,tools);
  t.mock.method(tools,'resolvePythonCommand',async(_root,_platform,env)=>({executable:env.PYTHON_AGENT_PYTHON || 'fixture-python',args:[]}));
  t.mock.method(tools,'command',async(exe,args)=>{if(args[0]==='info'&&!state.docker)throw new Error('Engine stopped');return args[0]==='info'?'linux':'ready';});
  t.mock.method(tools,'dockerCommand',async()=> 'fixture-docker');
  t.mock.method(tools,'sandboxImageReady',async()=>state.image);
  t.mock.method(tools,'ensureSandboxImage',async(log)=>{calls.push(['image']);state.image=true;log('Image ready.');return {docker:'fixture-docker',image:'fixture'};});
  t.mock.method(service,'probe',async(_python,env,selection)=>({sdkReady:selection.provider==='codex-subscription'||state.sdk,keyReady:selection.provider==='codex-subscription'||Boolean(env.GEMINI_API_KEY),keyName:selection.provider==='codex-subscription'?undefined:'GEMINI_API_KEY',label:selection.provider}));
  t.mock.method(tools,'runLogged',async(exe,args,_cwd,log)=>{
    calls.push([exe,...args]);log('Installing capability.');
    if(args[1]==='venv'){const python=path.join(args[2],process.platform==='win32'?'Scripts/python.exe':'bin/python');fs.mkdirSync(path.dirname(python),{recursive:true});fs.writeFileSync(python,'fixture');}
    if(args[1]==='pip')state.sdk=true;
  });
  return {root,service,state,calls,events,storage};
}

test('inspection is read-only and installation provisions only missing capabilities in a managed environment',async(t)=>{
  const {service,calls,events}=fixture(t);
  const before=await service.status(selection);assert.equal(before.ready,false);assert.deepEqual(calls,[]);
  assert.deepEqual(before.items.filter(item=>item.installable).map(item=>item.id),['provider','runtime','browser']);
  const after=await service.install(selection);
  const pip=calls.find(call=>call.includes('pip'));assert.equal(pip.at(-1),'google-genai');assert.match(pip[0],/settings.*python/);
  assert.equal(after.items.find(item=>item.id==='provider').ready,true);assert.equal(after.items.find(item=>item.id==='credentials').ready,false);
  assert.equal(service.snapshot().running,false);assert.ok(events.some(event=>event.log.includes('Image ready')));assert.equal(calls.some(call=>call[0]==='browser'),false);
  const count=calls.length;await service.install(selection);assert.equal(calls.length,count);
});

test('keys use encrypted host settings, never appear in readiness results, and reach only the selected helper environment',async(t)=>{
  const {root,service,state}=fixture(t);state.sdk=state.browser=state.image=true;
  await service.saveKey('gemini','test-secret-value');
  const disk=fs.readFileSync(path.join(root,'settings','provider-keys.json'),'utf8');assert.equal(disk.includes('test-secret-value'),false);
  const status=await service.status(selection);assert.equal(status.ready,true);assert.equal(status.keySaved,true);assert.equal(JSON.stringify(status).includes('test-secret-value'),false);
  const context={agentRoot:root,scriptName:'main.py',python:{executable:'base',args:[]},env:{KEEP:'yes'},options:selection};
  assert.equal((await service.hostContext(context)).env.GEMINI_API_KEY,'test-secret-value');
  assert.equal((await service.hostContext({...context,options:{provider:'codex-subscription',model:'gpt-5.4'}})).env.GEMINI_API_KEY,undefined);
  await service.saveKey('gemini',null);assert.equal((await service.status(selection)).keySaved,false);
});

test('missing Docker stays actionable while independent capabilities can install',async(t)=>{
  const {service,state,calls}=fixture(t);state.docker=false;
  const status=await service.install(selection);assert.equal(status.ready,false);assert.equal(status.items.find(item=>item.id==='docker').download,'docker');
  assert.equal(calls.some(call=>call[0]==='image'),false);assert.equal(calls.some(call=>call[0]==='browser'),false);
});

test('installation errors are visible, concurrent installs are rejected, and retry is possible',async(t)=>{
  const {service,state}=fixture(t);state.sdk=true;
  let release;const pending=new Promise(resolve=>release=resolve);
  t.mock.method(service.tools,'ensureSandboxImage',async()=>{await pending;throw new Error('Download unavailable');});
  const first=service.install(selection);await assert.rejects(service.install(selection),/already being installed/);release();
  await assert.rejects(first,/Download unavailable/);assert.equal(service.snapshot().running,false);assert.match(service.snapshot().error,/Download unavailable/);
  t.mock.method(service.tools,'ensureSandboxImage',async()=>{state.image=true;});await service.install(selection);assert.equal(service.snapshot().error,undefined);
});

test('unavailable encryption never falls back to storing plaintext keys',async(t)=>{
  const {service,storage,root}=fixture(t);storage.isEncryptionAvailable=()=>false;
  await assert.rejects(service.saveKey('gemini','secret'),/Secure credential storage is unavailable/);
  assert.equal(fs.existsSync(path.join(root,'settings','provider-keys.json')),false);
});


test('missing optional Playwright browser does not block artifact creation or require repair', async(t) => {
  const {service, state, calls} = fixture(t);
  state.sdk = state.image = true;
  await service.saveKey('gemini', 'test-secret');
  const status = await service.status(selection);
  assert.equal(status.ready, true);
  assert.deepEqual(calls, []);
  const browser = status.items.find(item => item.id === 'browser');
  assert.equal(browser.ready, false);
  assert.equal(browser.optional, true);
  assert.equal(browser.installable, true); // Retained for inspection tooling.
  await service.install(selection);
  assert.deepEqual(calls, []); // Optional inspection download is a separate action.
  state.image = false;
  assert.equal((await service.status(selection)).ready, false);
});
