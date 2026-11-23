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

export const artifactPreviewActionSchema = z.discriminatedUnion('operation', [
  z.strictObject({ operation: z.literal('restart') }),
  z.strictObject({ operation: z.literal('mcp'), input: artifactAppMcpRequestSchema.omit({ approveMutations: true }).strict() }),
  z.strictObject({ operation: z.literal('chat'), input: z.strictObject({ message: z.string().trim().min(1).max(4000) }) }),
]);
