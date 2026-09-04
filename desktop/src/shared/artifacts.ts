import { z } from 'zod';
import type { AgentEvent, AgentResponse, AgentStartOptions } from './contracts';
import type { JsonMap, RuntimeOptionsPayload } from './agentTypes';

export const artifactId = z.string().regex(/^[a-z0-9][a-z0-9-]{0,63}$/);
const shape = z.record(z.string(), z.unknown());
export const artifactApiSchema = z.object({
  id: artifactId,
  title: z.string().max(120).default(''),
  transport: z.enum(['http', 'websocket']),
  url: z.url().refine((value) => ['http:', 'https:', 'ws:', 'wss:'].includes(new URL(value).protocol), 'Use an HTTP or WebSocket URL.'),
  method: z.enum(['GET', 'POST', 'PUT', 'PATCH', 'DELETE']).default('GET'),
  requestSchema: shape.default({}),
  responseSchema: shape.default({}),
  headerEnv: z.record(z.string(), z.string().regex(/^[A-Za-z_][A-Za-z0-9_]*$/)).default({}),
}).superRefine((value, context) => {
  if ((value.transport === 'http') !== /^https?:/.test(value.url)) context.addIssue({ code: 'custom', message: 'Transport and URL must match.' });
  if (new URL(value.url).username || new URL(value.url).password) context.addIssue({ code: 'custom', message: 'Use environment variables for credentials.' });
});
export const artifactApisSchema = z.object({ version: z.literal(1), apis: z.array(artifactApiSchema).max(50) }).superRefine((value, context) => {
  if (new Set(value.apis.map((api) => api.id)).size !== value.apis.length) context.addIssue({ code: 'custom', message: 'API IDs must be unique.' });
});
export type ArtifactApiConfig = z.infer<typeof artifactApiSchema>;
export type ArtifactApis = z.infer<typeof artifactApisSchema>;
export const artifactAgentRuntimeSchema = z.object({ provider: z.string().min(1).max(80), model: z.string().min(1).max(200), backendScript: z.enum(['main.py', 'main_v2.py', 'live_test_loop.py']) });
export const readDirectorySchema = z.object({ id: artifactId.refine((id) => !['workspace', 'repo', 'context'].includes(id), 'This ID is reserved.'), label: z.string().min(1).max(200), path: z.string().min(1).max(4096), access: z.enum(['read', 'write']).default('read') });
export const artifactAccessSchema = z.object({ directories: z.array(readDirectorySchema).max(30).default([]), allowWorkspaceRead: z.boolean().default(false) }).superRefine((value, context) => {
  if (new Set(value.directories.map((item) => item.id)).size !== value.directories.length) context.addIssue({ code: 'custom', message: 'Folder IDs must be unique.' });
});
export type ReadDirectory = z.infer<typeof readDirectorySchema>;
export interface PrebuiltArtifact { id: string; title: string; description: string; requiresWriteAccess: boolean; }
export type ArtifactAccess = z.infer<typeof artifactAccessSchema>;
export const createArtifactSchema = z.object({ title: z.string().trim().min(1).max(120), prompt: z.string().trim().min(1).max(20000), sourceRoot: z.string().max(4096), shareFacts: z.boolean(), shareMemory: z.boolean(), runtime: artifactAgentRuntimeSchema.optional(), access: artifactAccessSchema.optional() });
export type CreateArtifact = z.infer<typeof createArtifactSchema>;
export interface ArtifactRecord extends CreateArtifact { id: string; root: string; createdAt: string; contextMode: 'links' | 'junction' | 'snapshot' | 'unavailable' | 'none'; contextWarning?: string }
export interface ArtifactLibrary { root: string; artifacts: ArtifactRecord[] }
export interface ArtifactRuntime { id: string; status: 'stopped' | 'installing' | 'starting' | 'running' | 'error'; url?: string; error?: string; logs: string }
export const artifactSetupSelectionSchema = z.object({ provider: z.enum(['openai', 'codex-subscription', 'gemini', 'anthropic', 'meta', 'local', 'ollama', 'ollama-local', 'ollama-runpod']), model: z.string().min(1).max(200) });
export type ArtifactSetupSelection = z.infer<typeof artifactSetupSelectionSchema>;
export type ArtifactCapabilityId = 'python' | 'git' | 'docker' | 'provider' | 'credentials' | 'browser' | 'runtime';
export interface ArtifactCapability { id: ArtifactCapabilityId; label: string; ready: boolean; detail: string; installable?: boolean; optional?: boolean; download?: 'python' | 'git' | 'docker'; }
export interface ArtifactCapabilities { selection: ArtifactSetupSelection; items: ArtifactCapability[]; ready: boolean; keyName?: string; keySaved: boolean; canSaveKey: boolean; }
export interface ArtifactSetupProgress { running: boolean; step: string; log: string; error?: string; }
export interface ArtifactDockerCleanupPlan { currentImage: string; obsoleteImages: string[]; orphanedVolumes: string[]; preservedImages: string[]; preservedVolumes: string[]; }
export interface ArtifactDockerCleanupResult extends ArtifactDockerCleanupPlan { removedImages: string[]; removedVolumes: string[]; failures: string[]; }
export type ArtifactEvent = { type: 'setup'; progress: ArtifactSetupProgress } | { type: 'runtime'; runtime: ArtifactRuntime } | { type: 'agent'; id: string; event: AgentEvent };
export const previewInputSchema = z.discriminatedUnion('type', [
  z.object({ type: z.literal('click'), x: z.number().min(0).max(3000), y: z.number().min(0).max(3000) }),
  z.object({ type: z.literal('wheel'), dx: z.number().min(-3000).max(3000), dy: z.number().min(-3000).max(3000) }),
  z.object({ type: z.literal('key'), key: z.string().min(1).max(80) }),
  z.object({ type: z.literal('text'), text: z.string().max(20000) }),
]);
export type PreviewInput = z.infer<typeof previewInputSchema>;
export interface PreviewFrame { image: string; width: number; height: number }
export interface ArtifactsApi {
  capabilities(selection: ArtifactSetupSelection): Promise<ArtifactCapabilities>;
  installCapabilities(selection: ArtifactSetupSelection): Promise<ArtifactCapabilities>;
  setupProgress(): Promise<ArtifactSetupProgress>;
  saveProviderKey(provider: ArtifactSetupSelection['provider'], key: string | null): Promise<void>;
  openSetupDownload(tool: 'python' | 'git' | 'docker'): Promise<void>;
  dockerCleanupPlan(): Promise<ArtifactDockerCleanupPlan>;
  cleanDocker(): Promise<ArtifactDockerCleanupResult>;
  library(): Promise<ArtifactLibrary>;
  chooseFolder(): Promise<ArtifactLibrary | null>;
  create(options: CreateArtifact): Promise<ArtifactRecord>;
  prebuilts(): Promise<PrebuiltArtifact[]>;
  installPrebuilt(id: string, access: ArtifactAccess, runtime?: z.infer<typeof artifactAgentRuntimeSchema>): Promise<ArtifactRecord>;
  chooseReadDirectory(): Promise<ReadDirectory | null>;
  access(id: string): Promise<ArtifactAccess>;
  saveAccess(id: string, access: ArtifactAccess): Promise<void>;
  apis(id: string): Promise<ArtifactApis>;
  saveApis(id: string, config: ArtifactApis): Promise<void>;
  start(id: string): Promise<ArtifactRuntime>;
  stop(id: string): Promise<void>;
  installBrowser(): Promise<void>;
  preview(id: string): Promise<PreviewFrame>;
  input(id: string, input: PreviewInput): Promise<void>;
  reload(id: string): Promise<void>;
  closePreview(id: string): Promise<void>;
  reveal(id: string): Promise<void>;
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
