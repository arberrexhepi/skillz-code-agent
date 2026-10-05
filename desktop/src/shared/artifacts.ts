import { z } from 'zod';
import type { AgentEvent, AgentResponse, AgentStartOptions, GitCommit, GitDiscardResult, GitFileDiff, GitStatus } from './contracts';
import type { JsonMap, RuntimeOptionsPayload } from './agentTypes';

export const artifactId = z.string().regex(/^[a-z0-9][a-z0-9-]{0,63}$/);
const shape = z.record(z.string(), z.unknown());
const headerEnvironment = z.union([z.string().regex(/^[A-Za-z_][A-Za-z0-9_]*$/), z.object({ env: z.string().regex(/^[A-Za-z_][A-Za-z0-9_]*$/), prefix: z.string().max(100).default('') })]);
export const artifactApiSchema = z.object({
  id: artifactId,
  title: z.string().max(120).default(''),
  description: z.string().max(500).default(''),
  collection: artifactId.default('default'),
  transport: z.enum(['http', 'websocket']),
  url: z.url().refine((value) => ['http:', 'https:', 'ws:', 'wss:'].includes(new URL(value).protocol), 'Use an HTTP or WebSocket URL.'),
  method: z.enum(['GET', 'POST', 'PUT', 'PATCH', 'DELETE']).default('GET'),
  requestSchema: shape.default({}),
  responseSchema: shape.default({}),
  headerEnv: z.record(z.string(), headerEnvironment).default({}),
  commandKind: z.enum(['discovery', 'mutation']).optional(),
  example: shape.default({}),
  tests: z.array(z.object({ path: z.string().max(500).default(''), operator: z.enum(['exists', 'equals', 'contains']), expected: z.unknown().optional() })).max(30).default([]),
  extract: z.array(z.object({ name: z.string().regex(/^[A-Za-z_][A-Za-z0-9_]*$/), path: z.string().min(1).max(500) })).max(30).default([]),
  scripts: z.array(z.discriminatedUnion('action', [
    z.object({ phase: z.literal('before'), action: z.literal('set'), name: z.string().regex(/^[A-Za-z_][A-Za-z0-9_]*$/), value: z.unknown() }),
    z.object({ phase: z.literal('after'), action: z.literal('extract'), name: z.string().regex(/^[A-Za-z_][A-Za-z0-9_]*$/), path: z.string().min(1).max(500) }),
    z.object({ phase: z.literal('after'), action: z.literal('assert'), path: z.string().max(500).default(''), operator: z.enum(['exists', 'equals', 'contains']), expected: z.unknown().optional() }),
  ])).max(50).default([]),
}).superRefine((value, context) => {
  if ((value.transport === 'http') !== /^https?:/.test(value.url)) context.addIssue({ code: 'custom', message: 'Transport and URL must match.' });
  if (new URL(value.url).username || new URL(value.url).password) context.addIssue({ code: 'custom', message: 'Use environment variables for credentials.' });
});
export const artifactApisSchema = z.object({ version: z.literal(1), apis: z.array(artifactApiSchema).max(50) }).superRefine((value, context) => {
  if (new Set(value.apis.map((api) => api.id)).size !== value.apis.length) context.addIssue({ code: 'custom', message: 'API IDs must be unique.' });
});
export type ArtifactApiConfig = z.infer<typeof artifactApiSchema>;
export type ArtifactApis = z.infer<typeof artifactApisSchema>;
export const artifactBlueprintChatMessageSchema = z.object({ role: z.enum(['user', 'assistant']), content: z.string().min(1).max(12000) });
export type ArtifactBlueprintChatMessage = z.infer<typeof artifactBlueprintChatMessageSchema>;
export interface ArtifactBlueprintAgentResult { message: string; config: ArtifactApis; changes: string[]; }
export const artifactDatabaseDialectSchema = z.enum(['sqlite', 'postgres', 'mysql', 'mariadb', 'mssql']);
export const artifactAppSchema = z.object({
  version: z.literal(1),
  chatbot: z.object({ enabled: z.boolean().default(false) }).default({ enabled: false }),
  database: z.object({
    enabled: z.boolean().default(false),
    dialect: artifactDatabaseDialectSchema.default('sqlite'),
    connectionEnv: z.string().regex(/^[A-Za-z_][A-Za-z0-9_]*$/).default('DATABASE_URL'),
    storage: z.string().min(1).max(500).default('.artifact-data/data.sqlite'),
  }).default({ enabled: false, dialect: 'sqlite', connectionEnv: 'DATABASE_URL', storage: '.artifact-data/data.sqlite' }),
});
export type ArtifactAppConfig = z.infer<typeof artifactAppSchema>;
export const defaultArtifactApp = (): ArtifactAppConfig => artifactAppSchema.parse({ version: 1 });
export const artifactAgentRuntimeSchema = z.object({ provider: z.string().min(1).max(80), model: z.string().min(1).max(200), backendScript: z.enum(['main.py', 'main_v2.py', 'live_test_loop.py']) });
export const packageScriptName = z.string().regex(/^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$/);
export const readDirectorySchema = z.object({ id: artifactId.refine((id) => !['workspace', 'repo', 'context'].includes(id), 'This ID is reserved.'), label: z.string().min(1).max(200), path: z.string().min(1).max(4096), access: z.enum(['read', 'write']).default('read'), allowProcessProxy: z.boolean().optional(), processProxyAllowlist: z.array(packageScriptName).max(100).optional() });
export const artifactAccessSchema = z.object({ directories: z.array(readDirectorySchema).max(30).default([]), allowWorkspaceRead: z.boolean().default(false), allowWorkspaceProcessProxy: z.boolean().optional(), workspaceProcessProxyAllowlist: z.array(packageScriptName).max(100).optional() }).superRefine((value, context) => {
  if (new Set(value.directories.map((item) => item.id)).size !== value.directories.length) context.addIssue({ code: 'custom', message: 'Folder IDs must be unique.' });
  if (value.allowWorkspaceProcessProxy && !value.allowWorkspaceRead) context.addIssue({ code: 'custom', message: 'Process Proxy for the active repository requires repository read access.' });
});
export type ReadDirectory = z.infer<typeof readDirectorySchema>;
export type ReadDirectoryChoice = ReadDirectory & { packageScripts: string[] };
export interface PrebuiltArtifact { id: string; title: string; description: string; requiresWriteAccess: boolean; requiresProcessProxy?: boolean; }
export type ArtifactAccess = z.infer<typeof artifactAccessSchema>;
export const createArtifactSchema = z.object({ title: z.string().trim().min(1).max(120), prompt: z.string().trim().min(1).max(20000), sourceRoot: z.string().max(4096), shareFacts: z.boolean(), shareMemory: z.boolean(), runtime: artifactAgentRuntimeSchema.optional(), access: artifactAccessSchema.optional(), app: artifactAppSchema.optional() });
export type CreateArtifact = z.infer<typeof createArtifactSchema>;
export interface ArtifactRecord extends CreateArtifact { id: string; root: string; createdAt: string; prebuiltId?: string; contextMode: 'links' | 'junction' | 'snapshot' | 'unavailable' | 'none'; contextWarning?: string }
export interface ArtifactLibrary { root: string; artifacts: ArtifactRecord[] }
export interface ArtifactRuntime { id: string; status: 'stopped' | 'installing' | 'starting' | 'running' | 'error'; url?: string; error?: string; logs: string }
export const artifactSetupSelectionSchema = z.object({ provider: z.enum(['openai', 'codex-subscription', 'gemini', 'anthropic', 'meta', 'local', 'ollama', 'ollama-local', 'ollama-runpod']), model: z.string().min(1).max(200) });
export type ArtifactSetupSelection = z.infer<typeof artifactSetupSelectionSchema>;
export const artifactVaultProviderSchema = z.enum(['openai', 'gemini', 'anthropic', 'meta']);
export type ArtifactVaultProvider = z.infer<typeof artifactVaultProviderSchema>;
export interface ArtifactVaultEntry { provider: ArtifactVaultProvider; label: string; keyName: string; source: 'saved' | 'environment' | 'missing'; }
export interface ArtifactVaultStatus { canSaveKey: boolean; entries: ArtifactVaultEntry[]; }
export type ArtifactCapabilityId = 'python' | 'git' | 'docker' | 'provider' | 'credentials' | 'browser' | 'runtime' | 'sqlite';
export interface ArtifactCapability { id: ArtifactCapabilityId; label: string; ready: boolean; detail: string; installable?: boolean; optional?: boolean; download?: 'python' | 'git' | 'docker'; }
export interface ArtifactCapabilities { selection: ArtifactSetupSelection; items: ArtifactCapability[]; ready: boolean; keyName?: string; keySaved: boolean; canSaveKey: boolean; }
export interface ArtifactSetupProgress { running: boolean; step: string; log: string; error?: string; }
export interface ArtifactDockerCleanupPlan { currentImage: string; obsoleteImages: string[]; orphanedVolumes: string[]; preservedImages: string[]; preservedVolumes: string[]; }
export interface ArtifactDockerCleanupResult extends ArtifactDockerCleanupPlan { removedImages: string[]; removedVolumes: string[]; failures: string[]; }
export interface ArtifactAppSaveResult { migrated: boolean; commit?: string; checkpointCommit?: string; }
export type ArtifactEvent = { type: 'setup'; progress: ArtifactSetupProgress } | { type: 'runtime'; runtime: ArtifactRuntime } | { type: 'apis'; id: string } | { type: 'agent'; id: string; event: AgentEvent };
export const previewInputSchema = z.discriminatedUnion('type', [
  z.object({ type: z.literal('click'), x: z.number().min(0).max(3000), y: z.number().min(0).max(3000) }),
  z.object({ type: z.literal('wheel'), dx: z.number().min(-3000).max(3000), dy: z.number().min(-3000).max(3000) }),
  z.object({ type: z.literal('key'), key: z.string().min(1).max(80) }),
  z.object({ type: z.literal('text'), text: z.string().max(20000) }),
]);
export type PreviewInput = z.infer<typeof previewInputSchema>;
export interface PreviewFrame { image: string; width: number; height: number }
export interface ArtifactsApi {
  capabilities(selection: ArtifactSetupSelection, artifactId?: string): Promise<ArtifactCapabilities>;
  installCapabilities(selection: ArtifactSetupSelection, artifactId?: string): Promise<ArtifactCapabilities>;
  setupProgress(): Promise<ArtifactSetupProgress>;
  vault(): Promise<ArtifactVaultStatus>;
  saveProviderKey(provider: ArtifactVaultProvider, key: string | null): Promise<void>;
  openSetupDownload(tool: 'python' | 'git' | 'docker'): Promise<void>;
  dockerCleanupPlan(): Promise<ArtifactDockerCleanupPlan>;
  cleanDocker(): Promise<ArtifactDockerCleanupResult>;
  library(): Promise<ArtifactLibrary>;
  chooseFolder(): Promise<ArtifactLibrary | null>;
  create(options: CreateArtifact): Promise<ArtifactRecord>;
  prebuilts(): Promise<PrebuiltArtifact[]>;
  installPrebuilt(id: string, access: ArtifactAccess, runtime?: z.infer<typeof artifactAgentRuntimeSchema>): Promise<ArtifactRecord>;
  chooseReadDirectory(): Promise<ReadDirectoryChoice | null>;
  access(id: string): Promise<ArtifactAccess>;
  processScripts(id: string): Promise<Record<string, string[]>>;
  saveAccess(id: string, access: ArtifactAccess): Promise<void>;
  apis(id: string): Promise<ArtifactApis>;
  saveApis(id: string, config: ArtifactApis): Promise<void>;
  blueprintAgent(id: string, message: string, history: ArtifactBlueprintChatMessage[], selection: ArtifactSetupSelection): Promise<ArtifactBlueprintAgentResult>;
  runApiCollection(id: string, collection: string, variables?: JsonMap, approveMutations?: boolean): Promise<JsonMap>;
  app(id: string): Promise<ArtifactAppConfig>;
  saveApp(id: string, config: ArtifactAppConfig): Promise<ArtifactAppSaveResult>;
  start(id: string): Promise<ArtifactRuntime>;
  stop(id: string): Promise<void>;
  installBrowser(): Promise<void>;
  preview(id: string): Promise<PreviewFrame>;
  input(id: string, input: PreviewInput): Promise<void>;
  reload(id: string): Promise<void>;
  closePreview(id: string): Promise<void>;
  reveal(id: string): Promise<void>;
  gitStatus(id: string): Promise<GitStatus>;
  gitInitialize(id: string): Promise<GitStatus>;
  gitHistory(id: string, limit?: number): Promise<GitCommit[]>;
  gitFileDiff(id: string, path: string, staged?: boolean): Promise<GitFileDiff>;
  gitStage(id: string, paths: string[]): Promise<GitStatus>;
  gitStageAll(id: string): Promise<GitStatus>;
  gitUnstage(id: string, paths: string[]): Promise<GitStatus>;
  gitDiscard(id: string, path: string): Promise<GitDiscardResult>;
  gitCommit(id: string, message: string): Promise<GitStatus>;
  gitPush(id: string): Promise<GitStatus>;
  agentStart(id: string, options: AgentStartOptions): Promise<AgentResponse>;
  agentSubmit(id: string, text: string): Promise<AgentResponse>;
  agentRuntimeOptions(id: string, provider?: string, model?: string): Promise<RuntimeOptionsPayload>;
  agentPlannerAction(id: string, action: string, extras?: JsonMap): Promise<AgentResponse>;
  agentWorkerAction(id: string, action: JsonMap): Promise<AgentResponse>;
  agentReconfigure(id: string, provider: string, model: string): Promise<AgentResponse>;
  agentBackoff(id: string, enabled: boolean, limit: number): Promise<AgentResponse>;
  agentStop(id: string): Promise<void>;
  onEvent(listener: (event: ArtifactEvent) => void): () => void;
}
