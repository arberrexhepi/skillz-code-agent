const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { test } = require('node:test');
const load = require('./load-ts.cjs');
const { ArtifactCapabilitiesService, ArtifactPreviewService } = load(()=>({...require('../src/main/services/artifactCapabilities.ts'),...require('../src/main/services/artifactPreview.ts')}));

test('real setup provisions a managed Gemini SDK and current Docker runtime, then reuses them', {timeout:240000}, async(t)=>{
  const root=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'skillz setup integration ')));
  const prior=process.env.PYTHON_AGENT_PYTHON;
  const base=execFileSync(process.platform==='win32'?'py':'python3',[...(process.platform==='win32'?['-3']:[]),'-c','import sys;print(sys.executable)'],{encoding:'utf8',windowsHide:true}).trim();
  process.env.PYTHON_AGENT_PYTHON=base;
  const preview=new ArtifactPreviewService();
  t.after(async()=>{if(prior===undefined)delete process.env.PYTHON_AGENT_PYTHON;else process.env.PYTHON_AGENT_PYTHON=prior;await preview.dispose();assert.equal(fs.realpathSync(path.dirname(root)),fs.realpathSync(os.tmpdir()));assert.ok(path.basename(root).startsWith('skillz setup integration '));await fs.promises.rm(root,{recursive:true,force:true,maxRetries:5});});
  const events=[];const service=new ArtifactCapabilitiesService(root,preview,progress=>{events.push(progress);},()=>path.resolve(__dirname,'../..'));
  const selection={provider:'gemini',model:'gemini-3-flash-preview'};
  const before=await service.status(selection);
  const after=await service.install(selection);
  for(const id of ['python','provider','git','docker','runtime'])assert.equal(after.items.find(item=>item.id===id).ready,true,id);
  if(!before.items.find(item=>item.id==='provider').ready)assert.ok(after.items.find(item=>item.id==='python').detail.startsWith(root));
  const again=await service.install(selection);assert.equal(again.items.some(item=>item.installable && !item.optional),false);
  assert.equal(service.snapshot().running,false);assert.ok(events.some(event=>event.running));
  console.log('Managed SDK and current Docker image are ready; no model calls made.');
});
