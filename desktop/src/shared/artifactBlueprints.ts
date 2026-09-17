import { artifactApisSchema, type ArtifactApiConfig } from './artifacts';

export interface ArtifactApiBlueprint { id: string; title: string; description: string; apis: ArtifactApiConfig[] }
const object = { type: 'object', additionalProperties: true };
const request = (value: Omit<ArtifactApiConfig, 'description' | 'collection' | 'requestSchema' | 'responseSchema' | 'headerEnv' | 'example' | 'tests' | 'extract' | 'scripts'> & Partial<ArtifactApiConfig>): ArtifactApiConfig => ({ description: '', collection: 'default', requestSchema: object, responseSchema: object, headerEnv: {}, example: {}, tests: [], extract: [], scripts: [], ...value });

export const artifactApiBlueprints: ArtifactApiBlueprint[] = [
  { id: 'github', title: 'GitHub repository', description: 'Read repository details and issues through the GitHub REST API.', apis: [
    request({ id: 'github-repository', title: 'Repository details', description: 'Get repository metadata.', collection: 'github', transport: 'http', url: 'https://api.github.com/repos/{{owner}}/{{repo}}', method: 'GET', requestSchema: { type: 'object', properties: { owner: { type: 'string' }, repo: { type: 'string' } }, required: ['owner', 'repo'], additionalProperties: false }, headerEnv: { Authorization: { env: 'GITHUB_TOKEN', prefix: 'Bearer ' } }, example: { owner: '{{owner}}', repo: '{{repo}}' }, tests: [{ path: 'full_name', operator: 'exists' }] }),
    request({ id: 'github-issues', title: 'Open issues', description: 'List open repository issues.', collection: 'github', transport: 'http', url: 'https://api.github.com/repos/{{owner}}/{{repo}}/issues', method: 'GET', requestSchema: { type: 'object', properties: { owner: { type: 'string' }, repo: { type: 'string' }, state: { type: 'string' } }, required: ['owner', 'repo'], additionalProperties: false }, responseSchema: { type: 'array' }, headerEnv: { Authorization: { env: 'GITHUB_TOKEN', prefix: 'Bearer ' } }, example: { owner: '{{owner}}', repo: '{{repo}}', state: 'open' }, tests: [{ path: '', operator: 'exists' }] }),
  ] },
  { id: 'openai', title: 'OpenAI models', description: 'List models available to an OpenAI API key.', apis: [request({ id: 'openai-models', title: 'List models', description: 'List models available to the configured account.', collection: 'openai', transport: 'http', url: 'https://api.openai.com/v1/models', method: 'GET', requestSchema: { type: 'object', additionalProperties: false }, headerEnv: { Authorization: { env: 'OPENAI_API_KEY', prefix: 'Bearer ' } }, tests: [{ path: 'data', operator: 'exists' }] })] },
  { id: 'stripe', title: 'Stripe customers', description: 'List Stripe customers with a restricted secret key.', apis: [request({ id: 'stripe-customers', title: 'List customers', description: 'List customers without changing Stripe data.', collection: 'stripe', transport: 'http', url: 'https://api.stripe.com/v1/customers', method: 'GET', requestSchema: { type: 'object', properties: { limit: { type: 'number' } }, additionalProperties: false }, headerEnv: { Authorization: { env: 'STRIPE_API_KEY', prefix: 'Bearer ' } }, example: { limit: 10 }, tests: [{ path: 'data', operator: 'exists' }] })] },
];

function slug(value: string): string { return value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 64) || 'request'; }
export function openApiToArtifactBlueprint(document: unknown, id = 'imported'): ArtifactApiBlueprint {
  if (!document || typeof document !== 'object' || Array.isArray(document)) throw new Error('OpenAPI document must be an object.');
  const source = document as { info?: { title?: unknown; description?: unknown }; servers?: Array<{ url?: unknown }>; paths?: Record<string, unknown> };
  const base = String(source.servers?.[0]?.url || '').replace(/\/$/, '');
  if (!/^https?:\/\//.test(base)) throw new Error('OpenAPI document needs an HTTP server URL.');
  const apis: ArtifactApiConfig[] = [];
  for (const [route, rawPath] of Object.entries(source.paths || {})) {
    if (!rawPath || typeof rawPath !== 'object' || Array.isArray(rawPath)) continue;
    for (const method of ['get', 'post', 'put', 'patch', 'delete'] as const) {
      const operation = (rawPath as Record<string, unknown>)[method]; if (!operation || typeof operation !== 'object' || Array.isArray(operation)) continue;
      const op = operation as { operationId?: unknown; summary?: unknown; description?: unknown; parameters?: Array<{ name?: unknown; required?: unknown; schema?: Record<string, unknown> }>; requestBody?: { content?: Record<string, { schema?: Record<string, unknown> }> }; responses?: Record<string, { content?: Record<string, { schema?: Record<string, unknown> }> }> };
      const operationId = slug(String(op.operationId || `${method}-${route}`));
      const url = base + route.replace(/\{([^}]+)\}/g, '{{$1}}');
      const response = Object.entries(op.responses || {}).find(([status]) => /^2/.test(status))?.[1];
      const body = op.requestBody?.content?.['application/json']?.schema || {}; const properties = { ...((body.properties as Record<string, unknown>) || {}) }; const required = new Set(Array.isArray(body.required) ? body.required.filter((item): item is string => typeof item === 'string') : []);
      for (const parameter of op.parameters || []) if (typeof parameter.name === 'string') { properties[parameter.name] = parameter.schema || {}; if (parameter.required) required.add(parameter.name); }
      for (const match of route.matchAll(/\{([^}]+)\}/g)) { properties[match[1]] ||= { type: 'string' }; required.add(match[1]); }
      const requestSchema = { type: 'object', properties, ...(required.size ? { required: [...required] } : {}), additionalProperties: false };
      apis.push(request({ id: operationId, title: String(op.summary || op.operationId || operationId), description: String(op.description || ''), collection: slug(id), transport: 'http', url, method: method.toUpperCase() as ArtifactApiConfig['method'], requestSchema, responseSchema: response?.content?.['application/json']?.schema || object }));
    }
  }
  if (!apis.length) throw new Error('No supported HTTP operations were found in the OpenAPI document.');
  return { id: slug(id), title: String(source.info?.title || 'Imported OpenAPI'), description: String(source.info?.description || 'Converted from OpenAPI.'), apis: artifactApisSchema.parse({ version: 1, apis }).apis };
}
