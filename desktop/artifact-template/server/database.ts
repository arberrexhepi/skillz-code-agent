import { mkdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import type { ArtifactAppConfig } from './schema';
import { artifactAppSchema } from './schema';

type SequelizeLike = { authenticate(): Promise<void>; query(sql: string, options?: Record<string, unknown>): Promise<[unknown, unknown]>; close(): Promise<void> };
let database: Promise<SequelizeLike | null> | undefined;

export async function appConfig(root: string): Promise<ArtifactAppConfig> {
  try { return artifactAppSchema.parse(JSON.parse(await readFile(path.join(root, '.artifact/app.json'), 'utf8'))); }
  catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return artifactAppSchema.parse({ version: 1 }); throw error; }
}

async function connect(root: string): Promise<SequelizeLike | null> {
  const config = await appConfig(root);
  if (!config.database.enabled) return null;
  const packageName = 'sequelize';
  const { Sequelize } = await import(packageName) as { Sequelize: new (...args: unknown[]) => SequelizeLike };
  let instance: SequelizeLike;
  if (config.database.dialect === 'sqlite') {
    const storage = path.resolve(root, config.database.storage);
    const relative = path.relative(root, storage);
    if (relative.startsWith('..') || path.isAbsolute(relative)) throw new Error('SQLite storage must stay inside the artifact repository.');
    await mkdir(path.dirname(storage), { recursive: true });
    instance = new Sequelize({ dialect: 'sqlite', storage, logging: false } as unknown);
  } else {
    const url = process.env[config.database.connectionEnv];
    if (!url) throw new Error(`Environment variable ${config.database.connectionEnv} is required for ${config.database.dialect}.`);
    instance = new Sequelize(url, { dialect: config.database.dialect, logging: false } as unknown);
  }
  await instance.authenticate();
  return instance;
}

export async function artifactDatabase(root: string): Promise<SequelizeLike> {
  database ||= connect(root);
  const value = await database;
  if (!value) throw new Error('Database capability is not enabled for this artifact.');
  return value;
}

export function databaseCommands(config: ArtifactAppConfig): Array<Record<string, unknown>> {
  if (!config.database.enabled) return [];
  return [
    { name: 'db_query', kind: 'discovery', description: 'Run a read-only SQL query through the configured Sequelize connection.', inputSchema: { type: 'object', properties: { sql: { type: 'string' }, replacements: { type: 'object' } }, required: ['sql'], additionalProperties: false } },
    { name: 'db_execute', kind: 'mutation', description: 'Run a data-changing SQL statement through the configured Sequelize connection.', inputSchema: { type: 'object', properties: { sql: { type: 'string' }, replacements: { type: 'object' } }, required: ['sql'], additionalProperties: false } },
  ];
}

function sqlInput(value: unknown): { sql: string; replacements: Record<string, unknown> } {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('SQL command input must be an object.');
  const { sql, replacements = {} } = value as { sql?: unknown; replacements?: unknown };
  if (typeof sql !== 'string' || !sql.trim() || sql.length > 100_000) throw new Error('Provide a SQL statement smaller than 100 KB.');
  if (!replacements || typeof replacements !== 'object' || Array.isArray(replacements)) throw new Error('SQL replacements must be an object.');
  return { sql: sql.trim(), replacements: replacements as Record<string, unknown> };
}

export async function executeDatabaseCommand(root: string, name: string, input: unknown): Promise<unknown> {
  const { sql, replacements } = sqlInput(input);
  if (name === 'db_query') {
    if (!/^(select|with|explain)\b/i.test(sql) || /\b(insert|update|delete|drop|alter|create|replace|truncate|attach|detach)\b/i.test(sql)) throw new Error('db_query accepts read-only SELECT, WITH, or EXPLAIN statements.');
  } else if (name !== 'db_execute') throw new Error('Unknown database command.');
  const [rows, metadata] = await (await artifactDatabase(root)).query(sql, { replacements });
  return { rows, metadata };
}

export async function closeDatabase(): Promise<void> {
  const pending = database; database = undefined;
  const value = await pending;
  await value?.close();
}
