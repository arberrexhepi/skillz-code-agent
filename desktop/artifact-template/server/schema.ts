import { z } from 'zod';
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
  scripts: z.array(z.discriminatedUnion('action', [z.object({ phase: z.literal('before'), action: z.literal('set'), name: z.string().regex(/^[A-Za-z_][A-Za-z0-9_]*$/), value: z.unknown() }), z.object({ phase: z.literal('after'), action: z.literal('extract'), name: z.string().regex(/^[A-Za-z_][A-Za-z0-9_]*$/), path: z.string().min(1).max(500) }), z.object({ phase: z.literal('after'), action: z.literal('assert'), path: z.string().max(500).default(''), operator: z.enum(['exists', 'equals', 'contains']), expected: z.unknown().optional() })])).max(50).default([]),
}).superRefine((value, context) => {
  if ((value.transport === 'http') !== /^https?:/.test(value.url)) context.addIssue({ code: 'custom', message: 'Transport and URL must match.' });
  if (new URL(value.url).username || new URL(value.url).password) context.addIssue({ code: 'custom', message: 'Use environment variables for credentials.' });
});
export const artifactApisSchema = z.object({ version: z.literal(1), apis: z.array(artifactApiSchema).max(50) }).superRefine((value, context) => {
  if (new Set(value.apis.map((api) => api.id)).size !== value.apis.length) context.addIssue({ code: 'custom', message: 'API IDs must be unique.' });
});
export type ArtifactApiConfig = z.infer<typeof artifactApiSchema>;
export type ArtifactApis = z.infer<typeof artifactApisSchema>;
export const artifactAppSchema = z.object({
  version: z.literal(1),
  chatbot: z.object({ enabled: z.boolean().default(false) }).default({ enabled: false }),
  database: z.object({ enabled: z.boolean().default(false), dialect: z.enum(['sqlite', 'postgres', 'mysql', 'mariadb', 'mssql']).default('sqlite'), connectionEnv: z.string().regex(/^[A-Za-z_][A-Za-z0-9_]*$/).default('DATABASE_URL'), storage: z.string().min(1).max(500).default('.artifact-data/data.sqlite') }).default({ enabled: false, dialect: 'sqlite', connectionEnv: 'DATABASE_URL', storage: '.artifact-data/data.sqlite' }),
});
export type ArtifactAppConfig = z.infer<typeof artifactAppSchema>;
