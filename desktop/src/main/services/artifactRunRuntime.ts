import { AgentService } from './agent';
import { ArtifactAgentExecution, type AgentLaunch } from './artifactAgent';
import { ArtifactRunSession } from './artifactRunSession';
import { ArtifactRuntimeBridge, type ArtifactRuntimeSetup } from './artifactRuntimeBridge';
import type { ArtifactSandbox } from './artifactSandbox';
import type { RuntimeSettingsService } from './runtimeSettings';
import { WorkspaceService } from './workspace';
import { runRepositories, scopeRunRepository } from './artifactRunRepositories';
import type { AgentStartOptions } from '../../shared/contracts';

interface RunRuntimeOptions extends Omit<ArtifactRuntimeSetup, 'repositories'> {
  directory: string;
  selection: AgentStartOptions;
  settings: RuntimeSettingsService;
  sandbox: () => Promise<ArtifactSandbox>;
  prepareHost: (context: AgentLaunch) => Promise<AgentLaunch>;
  log: (message: string) => void;
}

// Recording owns a fresh beta conversation; the editing agent and app chatbot
// keep their existing sessions and model brokers.
export function createArtifactRunBridge(options: RunRuntimeOptions): ArtifactRuntimeBridge {
  const session = new ArtifactRunSession(options.directory, options.selection, async (emit, repository) => {
    const workspace = new WorkspaceService(() => {});
    try {
      const scope = scopeRunRepository(await options.sandbox(), repository?.id);
      await workspace.open(scope.root);
      const execution = new ArtifactAgentExecution(async () => scope.sandbox, options.log, options.prepareHost);
      const agent = new AgentService(workspace, emit, options.settings, execution);
      return {
        start: selection => agent.start(selection),
        submit: text => agent.submit(scope.repository.id === 'repo' ? text : `Working repository: ${scope.repository.label}. File Access grants ${scope.repository.access === 'read' ? 'read-only access. Inspect this repository; do not modify it.' : 'read and write access. Make changes only as requested.'}\n\nUser request:\n${text}`),
        plannerAction: (action, extras) => agent.plannerAction(action, extras),
        stop: async () => { try { await agent.stop(); } finally { workspace.dispose(); } },
      };
    } catch (error) { workspace.dispose(); throw error; }
  });
  return new ArtifactRuntimeBridge(session, { ...options, repositories: async () => runRepositories(await options.sandbox()) });
}
