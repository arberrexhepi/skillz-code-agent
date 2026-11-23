import { randomUUID } from 'node:crypto';
import { constants, promises as fs } from 'node:fs';
import path from 'node:path';
import { z } from 'zod';
import { reasoningModelSchema, reasoningPreferencesSchema, type ReasoningEffort, type ReasoningModel, type ReasoningPhase, type RepoReasoningPreferences } from '../../shared/reasoningPreferences';

const filename = '.agent-reasoning.json';
const fileSchema = z.object({ version: z.literal(1), profiles: z.array(reasoningModelSchema.extend({ efforts: reasoningPreferencesSchema })) });
const writes = new Map<string, Promise<unknown>>();

async function readFile(root: string): Promise<z.infer<typeof fileSchema>> {
  try {
    const file = await fs.open(path.join(root, filename), constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0) | (constants.O_NONBLOCK ?? 0));
    try {
      if (!(await file.stat()).isFile()) throw new Error('Reasoning preferences must be a regular file.');
      return fileSchema.parse(JSON.parse(await file.readFile('utf8')));
    } finally { await file.close(); }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
    return { version: 1, profiles: [] };
  }
}

function sameModel(entry: ReasoningModel, selection: ReasoningModel): boolean {
  return entry.provider === selection.provider && entry.model === selection.model;
}

export async function readReasoningPreferences(root: string, selection: ReasoningModel): Promise<RepoReasoningPreferences> {
  await writes.get(root)?.catch(() => undefined);
  const { profiles } = await readFile(root);
  return { root, preferences: profiles.find(entry => sameModel(entry, selection))?.efforts ?? {} };
}

export function saveReasoningPreference(root: string, selection: ReasoningModel, phase: ReasoningPhase, effort: ReasoningEffort | null): Promise<RepoReasoningPreferences> {
  const pending = (writes.get(root) ?? Promise.resolve()).catch(() => undefined).then(async () => {
    const settings = await readFile(root);
    const index = settings.profiles.findIndex(entry => sameModel(entry, selection));
    const preferences = { ...settings.profiles[index]?.efforts };
    if (effort === null) delete preferences[phase];
    else preferences[phase] = effort;
    if (Object.keys(preferences).length) {
      const profile = { ...selection, efforts: preferences };
      if (index < 0) settings.profiles.push(profile);
      else settings.profiles[index] = profile;
    } else if (index >= 0) settings.profiles.splice(index, 1);
    else return { root, preferences };

    const temp = path.join(root, `${filename}.${randomUUID()}.tmp`);
    try {
      await fs.writeFile(temp, JSON.stringify(settings, null, 2) + '\n', { flag: 'wx' });
      await fs.rename(temp, path.join(root, filename));
    } finally { await fs.unlink(temp).catch(() => undefined); }
    return { root, preferences };
  });
  writes.set(root, pending);
  const clear = (): void => { if (writes.get(root) === pending) writes.delete(root); };
  void pending.then(clear, clear);
  return pending;
}
