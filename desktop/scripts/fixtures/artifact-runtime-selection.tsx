// Real Config and Agent panels; offline APIs record the exact launch payloads.
import { createRoot } from 'react-dom/client';
import { AgentWorkspaceProvider } from '../../src/renderer/src/agent/AgentWorkspaceContext';
import { ArtifactsWorkbench } from '../../src/renderer/src/components/ArtifactsWorkbench';
import type { AgentEvent, AgentStartOptions, WorkbenchApi } from '../../src/shared/contracts';
import type { ArtifactEvent, ArtifactLibrary } from '../../src/shared/artifacts';

const requests: Array<{ id: string; type: string; selection?: AgentStartOptions }> = [];
const artifactListeners = new Set<(event: ArtifactEvent) => void>();
const agentListeners = new Set<(event: AgentEvent) => void>();
const original = { provider: 'gemini', model: 'fixture-old', backendScript: 'main_v2.py' as const };
let failSave = false;
let library: ArtifactLibrary = { root: '/fixture', artifacts: ['one', 'two'].map(id => ({
  id, title: `Studio ${id}`, prompt: `Build studio ${id}`, root: `/fixture/${id}`, createdAt: '2026-01-01',
  sourceRoot: '', shareFacts: false, shareMemory: false, contextMode: 'none', runtime: { ...original },
})) };
const emit = (event: ArtifactEvent) => artifactListeners.forEach(listener => listener(event));
const response = (id: string) => ({ ok: true, state: { planner: {}, transcript: [{ role: 'assistant' as const, content: `Preserved conversation ${id}` }] } });
const options = async (provider = 'gemini', model = 'fixture-old') => ({
  current_provider: provider, current_model: model,
  providers: [
    { key: 'gemini', label: 'Gemini fixture', default_model: 'fixture-old', suggested_models: ['fixture-old', 'fixture-new'] },
    { key: 'openai', label: 'OpenAI fixture', default_model: 'fixture-openai', suggested_models: ['fixture-openai'] },
  ],
});

window.workbench = {
  agent: {
    runtimeOptions: options, onEvent: listener => { agentListeners.add(listener); return () => { agentListeners.delete(listener); }; },
    start: async selection => { requests.push({ id: 'source', type: 'start', selection }); return response('source'); },
  },
  artifacts: {
    library: async () => structuredClone(library), prebuilts: async () => [],
    onEvent: listener => { artifactListeners.add(listener); return () => { artifactListeners.delete(listener); }; },
    capabilities: async selection => ({ selection, ready: true, items: [], keySaved: true, canSaveKey: true }),
    setupProgress: async () => ({ running: false, step: '', log: '' }),
    saveRuntime: async (id, runtime) => {
      if (failSave) throw new Error('Fixture runtime save failed');
      const record = { ...library.artifacts.find(item => item.id === id)!, runtime: { ...runtime } };
      library = { ...library, artifacts: library.artifacts.map(item => item.id === id ? record : item) };
      requests.push({ id, type: 'save', selection: runtime });
      emit({ type: 'runtime-selection', record });
      return record;
    },
    agentRuntimeOptions: (_id, provider, model) => options(provider, model),
    agentStart: async (id, selection) => {
      requests.push({ id, type: 'start', selection });
      emit({ type: 'agent', id, event: { type: 'status', status: 'running' } });
      return response(id);
    },
    agentStop: async id => { requests.push({ id, type: 'stop' }); emit({ type: 'agent', id, event: { type: 'status', status: 'stopped' } }); },
    agentSubmit: async id => { requests.push({ id, type: 'submit' }); return response(id); },
    agentReconfigure: async id => { requests.push({ id, type: 'reconfigure' }); return response(id); },
    closePreview: async () => {},
  },
} as WorkbenchApi;
Object.assign(window, { artifactRuntimeFixture: {
  requests, failSave: () => { failSave = true; }, emitArtifact: emit,
  emitSourceAgent: (event: AgentEvent) => agentListeners.forEach(listener => listener(event)),
} });

createRoot(document.getElementById('root')!).render(
  <AgentWorkspaceProvider initialRuntime={{ provider: 'gemini', model: 'source-model', backendScript: 'main.py' }}>
    <ArtifactsWorkbench visible sourceRoot="" onClose={() => {}} />
  </AgentWorkspaceProvider>,
);
