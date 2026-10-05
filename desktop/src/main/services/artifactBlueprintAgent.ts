import { z } from 'zod';
import {
  artifactApiSchema,
  artifactApisSchema,
  type ArtifactApis,
  type ArtifactBlueprintAgentResult,
  type ArtifactBlueprintChatMessage,
  type ArtifactSetupSelection,
} from '../../shared/artifacts';

const blueprintOperationSchema = z.discriminatedUnion('operation', [
  z.object({ operation: z.literal('create'), api: artifactApiSchema }),
  z.object({ operation: z.literal('update'), id: z.string().min(1).max(64), api: artifactApiSchema }),
  z.object({ operation: z.literal('delete'), id: z.string().min(1).max(64) }),
]);

const blueprintAgentOutputSchema = z.object({
  message: z.string().trim().min(1).max(6000),
  operations: z.array(blueprintOperationSchema).max(20).default([]),
});

export type ArtifactBlueprintOperation = z.infer<typeof blueprintOperationSchema>;

function responseJson(text: string): unknown {
  const trimmed = text.trim();
  const fence = String.fromCharCode(96).repeat(3);
  let candidate = trimmed;
  if (trimmed.startsWith(fence)) {
    const firstNewline = trimmed.indexOf('\n');
    const lastFence = trimmed.lastIndexOf(fence);
    if (firstNewline >= 0 && lastFence > firstNewline) candidate = trimmed.slice(firstNewline + 1, lastFence).trim();
  }
  try { return JSON.parse(candidate); }
  catch { throw new Error('Blueprint mode returned an invalid response. Retry or make the request more specific.'); }
}

export function applyBlueprintOperations(current: ArtifactApis, operations: ArtifactBlueprintOperation[]): { config: ArtifactApis; changes: string[] } {
  const apis = [...current.apis];
  const changes: string[] = [];
  for (const operation of operations) {
    const index = apis.findIndex((api) => api.id === (operation.operation === 'create' ? operation.api.id : operation.id));
    if (operation.operation === 'create') {
      if (index >= 0) throw new Error('API Blueprint ' + operation.api.id + ' already exists.');
      apis.push(operation.api);
      changes.push('Created ' + operation.api.id);
      continue;
    }
    if (index < 0) throw new Error('API Blueprint ' + operation.id + ' no longer exists.');
    if (operation.operation === 'delete') {
      apis.splice(index, 1);
      changes.push('Deleted ' + operation.id);
      continue;
    }
    const conflict = apis.findIndex((api, candidate) => candidate !== index && api.id === operation.api.id);
    if (conflict >= 0) throw new Error('API Blueprint ' + operation.api.id + ' already exists.');
    apis[index] = operation.api;
    changes.push('Updated ' + operation.id + (operation.id === operation.api.id ? '' : ' as ' + operation.api.id));
  }
  return { config: artifactApisSchema.parse({ version: 1, apis }), changes };
}

export async function runArtifactBlueprintTurn(options: {
  current: ArtifactApis;
  message: string;
  history: ArtifactBlueprintChatMessage[];
  selection: ArtifactSetupSelection;
  request: (selection: ArtifactSetupSelection, payload: Record<string, unknown>) => Promise<Record<string, unknown>>;
}): Promise<ArtifactBlueprintAgentResult> {
  const system = [
    'You are the API Blueprint manager for one Skillz artifact.',
    'This is a constrained configuration mode. You may only inspect the supplied API Blueprints and propose validated create, update, or delete operations. You cannot edit application source files, run shell commands, or manage credentials.',
    'Return strict JSON with this shape: {"message":"plain-language response","operations":[...]}.',
    'Operations are {"operation":"create","api":FULL_API}, {"operation":"update","id":"CURRENT_ID","api":FULL_RESULTING_API}, or {"operation":"delete","id":"CURRENT_ID"}.',
    'For an update, return the complete resulting API and preserve every unchanged field. Use no operations when answering a read-only question or when the request is ambiguous.',
    'Every API requires: id, title, description, collection, transport, url, method, requestSchema, responseSchema, headerEnv, example, tests, extract, and scripts. commandKind may be omitted.',
    'Field shapes: requestSchema, responseSchema, and example are JSON objects. headerEnv maps a header to an environment variable name string or {env,prefix}. tests use {path,operator,expected?}, where operator is exists, equals, or contains. extract uses {name,path}. scripts use {phase:"before",action:"set",name,value}, {phase:"after",action:"extract",name,path}, or {phase:"after",action:"assert",path,operator,expected?}.',
    'IDs use lowercase letters, digits, and hyphens. HTTP transport requires http/https URLs; websocket requires ws/wss. URLs must not contain credentials.',
    'Secrets are referenced only through headerEnv environment-variable names. Never put a credential value in a Blueprint or your response.',
    'Maximum 20 operations. Explain exactly what will change in message.',
    'Current validated API Blueprints:\n' + JSON.stringify(options.current),
  ].join('\n\n');
  const history = options.history.slice(-20).map(({ role, content }) => ({ role, content }));
  const response = await options.request(options.selection, {
    system,
    messages: [...history, { role: 'user', content: options.message }],
  });
  const output = blueprintAgentOutputSchema.parse(responseJson(String(response.text || '')));
  const applied = applyBlueprintOperations(options.current, output.operations);
  return { message: output.message, ...applied };
}