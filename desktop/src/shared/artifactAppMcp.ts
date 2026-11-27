import { z } from 'zod';

export const artifactMcpName = z.string().regex(/^[a-z0-9][a-z0-9-]{0,63}$/);
const environment = z.string().regex(/^[A-Za-z_][A-Za-z0-9_]*$/).refine(
  name => !/^SKILLZ_/i.test(name) && !['PATH', 'HOME'].includes(name.toUpperCase()),
  'Use an application credential or setting name.',
);
const localHosts = new Set(['localhost', '127.0.0.1', '[::1]']);
const hostAliases = new Set(['host.docker.internal', 'gateway.docker.internal', 'host.containers.internal']);
const endpoint = z.url().max(4096).refine(value => {
  const url = new URL(value);
  const host = url.hostname.replace(/\.$/, '').toLowerCase();
  return !url.username && !url.password && !url.hash && !hostAliases.has(host)
    && (localHosts.has(host) || !/^(?:\[|[0-9.]+$)/.test(host))
    && (url.protocol === 'https:' || (url.protocol === 'http:' && localHosts.has(host)));
}, 'Use an HTTPS MCP URL or HTTP loopback inside the artifact container.');
const common = {
  id: artifactMcpName, enabled: z.boolean().default(true), chatbot: z.boolean().default(false),
  allowedTools: z.array(z.string().min(1).max(200)).max(200).optional(),
  timeout: z.number().int().min(5).max(120).default(30),
};
export const artifactAppMcpServerSchema = z.union([
  z.strictObject({
    ...common, transport: z.literal('stdio'), source: z.literal('artifact'),
    env: z.array(environment).max(30).default([]),
  }),
  z.strictObject({
    ...common, transport: z.literal('stdio'), command: z.string().trim().min(1).max(1000),
    args: z.array(z.string().max(4096)).max(50).default([]),
    env: z.array(environment).max(30).default([]),
    source: z.never().optional(),
  }),
  z.strictObject({
    ...common, transport: z.literal('http'), url: endpoint, bearerEnv: environment.optional(),
    source: z.never().optional(),
  }),
]);
export const artifactAppMcpSchema = z.object({
  enabled: z.boolean().default(false), servers: z.array(artifactAppMcpServerSchema).max(24).default([]),
}).superRefine((value, context) => {
  if (new Set(value.servers.map(server => server.id)).size !== value.servers.length) {
    context.addIssue({ code: 'custom', path: ['servers'], message: 'MCP server names must be unique.' });
  }
});
export type ArtifactAppMcp = z.infer<typeof artifactAppMcpSchema>;
export type ArtifactAppMcpServer = z.infer<typeof artifactAppMcpServerSchema>;
export const artifactMcpSourcePath = (id: string) => `mcp/${id}/server.py`;

export const artifactAppMcpRequestSchema = z.object({
  operation: z.enum(['servers', 'list', 'search', 'describe', 'call', 'read-resource', 'get-prompt']),
  server: artifactMcpName.optional(),
  kind: z.enum(['tools', 'resources', 'templates', 'prompts']).optional(),
  query: z.string().max(200).optional(),
  offset: z.number().int().min(0).optional(),
  name: z.string().min(1).max(1024).optional(),
  uri: z.string().min(1).max(4096).optional(),
  arguments: z.record(z.string(), z.unknown()).optional(),
  approveMutations: z.boolean().optional(),
});
export type ArtifactAppMcpRequest = z.infer<typeof artifactAppMcpRequestSchema>;

const previewRoute = z.string().max(4096).regex(/^\/(?![\/\\])[^\\\r\n]*$/);
const browserSelector = z.string().trim().min(1).max(500);
const browserLabel = z.string().trim().min(1).max(140);
const optionalBrowserLabel = browserLabel.optional();
const browserStepTimeout = z.number().int().min(100).max(90000).optional();
const browserViewport = z.strictObject({
  width: z.number().int().min(320).max(2560),
  height: z.number().int().min(240).max(1600),
});
const browserCheckStep = z.discriminatedUnion('type', [
  z.strictObject({ type: z.literal('assert-visible'), selector: browserSelector, label: optionalBrowserLabel, timeoutMs: browserStepTimeout }),
  z.strictObject({ type: z.literal('assert-hidden'), selector: browserSelector, label: optionalBrowserLabel, timeoutMs: browserStepTimeout }),
  z.strictObject({
    type: z.literal('assert-text'),
    selector: browserSelector,
    text: z.string().min(1).max(500),
    exact: z.boolean().optional(),
    label: optionalBrowserLabel,
    timeoutMs: browserStepTimeout,
  }),
  z.strictObject({ type: z.literal('assert-focused'), selector: browserSelector, label: optionalBrowserLabel, timeoutMs: browserStepTimeout }),
  z.strictObject({ type: z.literal('click'), selector: browserSelector, label: optionalBrowserLabel, timeoutMs: browserStepTimeout }),
  z.strictObject({ type: z.literal('fill'), selector: browserSelector, value: z.string().max(4000), label: optionalBrowserLabel, timeoutMs: browserStepTimeout }),
  z.strictObject({ type: z.literal('select'), selector: browserSelector, value: z.string().max(500), label: optionalBrowserLabel, timeoutMs: browserStepTimeout }),
  z.strictObject({
    type: z.literal('press'),
    key: z.string().trim().min(1).max(60),
    selector: browserSelector.optional(),
    label: optionalBrowserLabel,
    timeoutMs: browserStepTimeout,
  }),
  z.strictObject({ type: z.literal('hover'), selector: browserSelector, label: optionalBrowserLabel, timeoutMs: browserStepTimeout }),
  z.strictObject({
    type: z.literal('drag'),
    selector: browserSelector,
    dx: z.number().finite().min(-2000).max(2000),
    dy: z.number().finite().min(-2000).max(2000),
    steps: z.number().int().min(1).max(30).optional(),
    label: optionalBrowserLabel,
    timeoutMs: browserStepTimeout,
  }),
  z.strictObject({
    type: z.literal('wheel'),
    selector: browserSelector,
    deltaX: z.number().finite().min(-4000).max(4000).optional(),
    deltaY: z.number().finite().min(-4000).max(4000),
    label: optionalBrowserLabel,
    timeoutMs: browserStepTimeout,
  }),
  z.strictObject({ type: z.literal('wait'), ms: z.number().int().min(1).max(60000), label: optionalBrowserLabel }),
  z.strictObject({ type: z.literal('goto'), route: previewRoute, label: optionalBrowserLabel, timeoutMs: browserStepTimeout }),
  z.strictObject({ type: z.literal('evaluate'), script: z.string().trim().min(1).max(12000), label: optionalBrowserLabel, timeoutMs: browserStepTimeout }),
]);
export type ArtifactBrowserCheckStep = z.infer<typeof browserCheckStep>;
const browserAssertionStepTypes = new Set<ArtifactBrowserCheckStep['type']>([
  'assert-visible', 'assert-hidden', 'assert-text', 'assert-focused', 'evaluate',
]);
export const browserCheckInputSchema = z.strictObject({
  route: previewRoute,
  viewport: browserViewport.optional(),
  reducedMotion: z.enum(['reduce', 'no-preference']).optional(),
  colorScheme: z.enum(['light', 'dark', 'no-preference']).optional(),
  timeoutMs: z.number().int().min(1000).max(90000).optional(),
  steps: z.array(browserCheckStep).min(1).max(24).refine(
    steps => steps.some(step => browserAssertionStepTypes.has(step.type)),
    'Include at least one assertion or evaluate step.',
  ),
});
export type ArtifactBrowserCheckInput = z.infer<typeof browserCheckInputSchema>;

export const artifactPreviewActionSchema = z.discriminatedUnion('operation', [
  z.strictObject({ operation: z.literal('restart') }),
  z.strictObject({ operation: z.literal('route-check'), route: previewRoute }),
  z.strictObject({ operation: z.literal('browser-check'), input: browserCheckInputSchema }),
  z.strictObject({ operation: z.literal('mcp'), input: artifactAppMcpRequestSchema.omit({ approveMutations: true }).strict() }),
  z.strictObject({ operation: z.literal('chat'), input: z.strictObject({ message: z.string().trim().min(1).max(4000) }) }),
]);
