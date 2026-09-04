import { z } from 'zod';
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
