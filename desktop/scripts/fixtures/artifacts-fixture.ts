import type { ArtifactEvent, ArtifactLibrary, ArtifactRuntime, ArtifactApis } from '../../src/shared/artifacts';
import type { AgentBridgeState } from '../../src/shared/contracts';
let library: ArtifactLibrary = { root: '', artifacts: [] };
const listeners = new Set<(event: ArtifactEvent) => void>();
const selections = new Map<string, import('../../src/shared/contracts').AgentStartOptions>();
const configs = new Map<string, ArtifactApis>();
const states = new Map<string, AgentBridgeState>();
const emit = (event: ArtifactEvent) => listeners.forEach((listener) => listener(event));
const state = (id: string) => states.get(id) || { planner: {}, transcript: [] };
const response = (id: string) => ({ ok: true, state: state(id) });
let installed = false, keySaved = false, inspectionInstalled = false;
let setupProgress: import('../../src/shared/artifacts').ArtifactSetupProgress = { running: false, step: '', log: '' };
function capabilities(selection: import('../../src/shared/artifacts').ArtifactSetupSelection): import('../../src/shared/artifacts').ArtifactCapabilities {
  const keyName = selection.provider === 'gemini' ? 'GEMINI_API_KEY' : selection.provider === 'openai' ? 'OPENAI_API_KEY' : undefined;
  const items: import('../../src/shared/artifacts').ArtifactCapability[] = [
    {id:'python',label:'Python',ready:true,detail:'Python 3 ready'}, {id:'git',label:'Git',ready:true,detail:'Ready to version your artifacts'},
    {id:'docker',label:'Docker',ready:true,detail:'Linux container engine running'},
    {id:'provider',label:'Provider support',ready:installed || selection.provider==='codex-subscription',detail:installed?'Provider SDK installed':'Install support for your selected runtime',installable:!installed && selection.provider!=='codex-subscription'},
    {id:'browser',label:'Playwright inspection browser',ready:inspectionInstalled,optional:true,detail:'Optional for inspection; live previews use the built-in browser',installable:!inspectionInstalled},
    {id:'runtime',label:'Artifact runtime',ready:installed,detail:'Isolated artifact runtime',installable:!installed},
  ];
  if(keyName)items.push({id:'credentials',label:'API key',ready:keySaved,detail:keySaved?'Configured. The key has not been validated with the provider.':`Add ${keyName} below to connect this provider.`});
  return {selection,items,ready:items.every(item=>item.ready || item.optional),keyName,keySaved,canSaveKey:true};
}
window.workbench.artifacts = {
  capabilities: async(selection)=>capabilities(selection),
  installCapabilities: async(selection)=>{setupProgress={running:true,step:'Installing artifact capabilities',log:'Preparing capabilities…'};emit({type:'setup',progress:setupProgress});await new Promise(resolve=>setTimeout(resolve,500));installed=true;setupProgress={running:false,step:'Downloads complete',log:'Capabilities installed.'};emit({type:'setup',progress:setupProgress});return capabilities(selection);},
  setupProgress: async()=>setupProgress,
  saveProviderKey: async(_provider,key)=>{keySaved=Boolean(key);},
  openSetupDownload: async()=>{},
  library: async () => structuredClone(library),
  prebuilts: async () => [{id:'repo-issue-manager',title:'Repository issue manager',description:'Manage issues in explicitly shared repositories.',requiresWriteAccess:true}],
  installPrebuilt: async (_presetId, access, runtime) => { const id = `issue-manager-${library.artifacts.length + 1}`; const record = { id, root: `${library.root}/${id}`, title: 'Repository issue manager', prompt: 'Manage issues', createdAt: new Date().toISOString(), sourceRoot: '', shareFacts: false, shareMemory: false, contextMode: 'none' as const, access, runtime }; library = { ...library, artifacts: [...library.artifacts, record] }; return record; },
  chooseFolder: async () => { library = { root: '/artifacts-library', artifacts: [] }; return library; },
  create: async (options) => { const id = `artifact-${library.artifacts.length + 1}`; const record = { ...options, id, root: `${library.root}/${id}`, createdAt: new Date().toISOString(), contextMode: options.shareFacts || options.shareMemory ? 'snapshot' as const : 'none' as const }; library = { ...library, artifacts: [...library.artifacts, record] }; return record; },
  chooseReadDirectory: async () => ({ id: 'documents', label: 'Documents', path: 'C:\\Users\\example\\Documents', access: 'read' as const }),
  access: async (id) => library.artifacts.find((record) => record.id === id)?.access || { directories: [], allowWorkspaceRead: false },
  saveAccess: async (id, access) => { const record = library.artifacts.find((item) => item.id === id); if (record) record.access = access; emit({ type: 'runtime', runtime: { id, status: 'stopped', logs: '' } }); emit({ type: 'agent', id, event: { type: 'status', status: 'stopped' } }); },
  apis: async (id) => configs.get(id) || { version: 1, apis: [] }, saveApis: async (id, config) => { configs.set(id, config); },
  start: async (id) => { const {url} = await (await fetch('/__fixture-artifact-url')).json(); const runtime: ArtifactRuntime = { id, status: 'running', logs: 'Artifact listening on an available port.', url: `${url}/?id=${id}` }; emit({ type: 'runtime', runtime }); return runtime; },
  stop: async (id) => { emit({ type: 'runtime', runtime: { id, status: 'stopped', logs: '' } }); },
  installBrowser: async () => { inspectionInstalled = true; },
  preview: async (id) => ({ image: `data:image/svg+xml;base64,${btoa(`<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800"><rect width="1200" height="800" fill="#111924"/><text x="60" y="90" fill="#b0deca" font-family="sans-serif" font-size="36">${id} preview fixture</text><rect x="60" y="150" width="340" height="210" rx="12" fill="#223628"/><text x="85" y="195" font-family="sans-serif" font-size="24" fill="white">users</text><text x="85" y="240" font-family="monospace" font-size="19" fill="#abc4b5">id   integer</text><text x="85" y="280" font-family="monospace" font-size="19" fill="#abc4b5">name text</text></svg>`)}`, width: 1200, height: 800 }),
  input: async () => {}, reload: async () => {}, closePreview: async () => {}, reveal: async () => {},
  agentRuntimeOptions: async (_id, provider, model) => ({ current_provider: provider, current_model: model }),
  agentStart: async (id, options) => { selections.set(id, options); emit({ type: 'agent', id, event: { type: 'status', status: 'running' } }); return response(id); },
  agentSubmit: async (id, text) => { states.set(id, { planner: {}, transcript: [...state(id).transcript, { role: 'user', content: text }, { role: 'assistant', content: `Building this request in ${id} using ${selections.get(id)?.provider} · ${selections.get(id)?.model} · ${selections.get(id)?.backendScript}.` }] }); return response(id); },
  agentPlannerAction: async (id) => response(id), agentWorkerAction: async (id) => response(id), agentReconfigure: async (id) => response(id), agentBackoff: async (id) => response(id), agentStop: async (id) => { emit({ type: 'agent', id, event: { type: 'status', status: 'stopped' } }); },
  onEvent: (listener) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
};
