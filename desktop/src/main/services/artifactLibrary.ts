import { promises as fs } from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { artifactId, artifactApisSchema, artifactAgentRuntimeSchema, artifactAccessSchema, artifactAppSchema, defaultArtifactApp, type ArtifactAccess, createArtifactSchema, type ArtifactApis, type ArtifactAppConfig, type ArtifactAppSaveResult, type ArtifactLibrary, type ArtifactRecord, type CreateArtifact, type PrebuiltArtifact } from '../../shared/artifacts';
import { git } from './artifactProcess';

const manifestName = '.skillz-artifacts.json';
const author = ['-c', 'user.name=skillz Workbench', '-c', 'user.email=workbench@localhost'];
async function emptyArtifactDirectory(directory: string): Promise<boolean> {
  // Finder may write this metadata as soon as the user opens an otherwise empty folder.
  return (await fs.readdir(directory, { withFileTypes: true })).every(entry => entry.name === '.DS_Store' && entry.isFile());
}

export async function writeJson(file: string, value: unknown): Promise<void> {
  const temp = `${file}.${randomUUID()}.tmp`;
  await fs.mkdir(path.dirname(file), { recursive: true });
  try { await fs.writeFile(temp, JSON.stringify(value, null, 2) + '\n', { flag: 'wx', mode: 0o600 }); await fs.rename(temp, file); }
  finally { await fs.rm(temp, { force: true }); }
}
export async function readJson(file: string): Promise<unknown> {
  const stat = await fs.lstat(file);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 256000) throw new Error(`Expected a regular JSON file smaller than 256 KB: ${path.basename(file)}`);
  return JSON.parse(await fs.readFile(file, 'utf8'));
}
async function writeText(file: string, value: string): Promise<void> {
  const temp = `${file}.${randomUUID()}.tmp`;
  await fs.mkdir(path.dirname(file), { recursive: true });
  try { await fs.writeFile(temp, value, { flag: 'wx' }); await fs.rename(temp, file); }
  finally { await fs.rm(temp, { force: true }); }
}
export class ArtifactLibraryService {
  private queue: Promise<unknown> = Promise.resolve();
  constructor(private readonly settingsFile: string, private readonly template: string, private readonly contextHome: string, private readonly prebuiltHome = path.join(path.dirname(template), 'prebuilt-artifacts')) {}
  private serial<T>(operation: () => Promise<T>): Promise<T> { const pending = this.queue.then(operation); this.queue = pending.catch(() => {}); return pending; }
  async library(): Promise<ArtifactLibrary> {
    let settings: { root?: string };
    try { settings = await readJson(this.settingsFile) as { root?: string }; }
    catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return { root: '', artifacts: [] }; throw error; }
    if (!settings.root || !path.isAbsolute(settings.root)) throw new Error('Invalid artifact library setting.');
    const root = await fs.realpath(settings.root);
    const manifest = await readJson(path.join(root, manifestName)) as { version: number; artifacts: string[] };
    if (manifest.version !== 1 || !Array.isArray(manifest.artifacts)) throw new Error('Invalid artifact library manifest.');
    const artifacts: ArtifactRecord[] = [];
    const permissions = await this.readPermissions();
    const records = await this.readLocalRecords();
    for (const rawId of manifest.artifacts) {
      const id = artifactId.parse(rawId);
      const directory = await this.directory(root, id);
      const meta = await readJson(path.join(directory, 'artifact.json')) as { title: string; prompt: string; createdAt: string };
      // Imported/legacy repositories cannot grant themselves access by editing local JSON.
      const local = records[directory] || { sourceRoot: '', shareFacts: false, shareMemory: false, contextMode: 'none' };
      const access = permissions[directory] || { directories: [], allowWorkspaceRead: false };
      const app = await this.appForRoot(directory);
      artifacts.push({ id, root: directory, title: meta.title, prompt: meta.prompt, createdAt: meta.createdAt, sourceRoot: local.sourceRoot || '', shareFacts: Boolean(local.shareFacts), shareMemory: Boolean(local.shareMemory), contextMode: local.contextMode || 'none', contextWarning: local.contextWarning, runtime: artifactAgentRuntimeSchema.optional().parse(local.runtime), access: artifactAccessSchema.parse(access), app });
    }
    return { root, artifacts };
  }
  async find(id: string): Promise<ArtifactRecord> {
    artifactId.parse(id);
    const artifact = (await this.library()).artifacts.find((item) => item.id === id);
    if (!artifact) throw new Error('Artifact is not in the configured library.');
    return artifact;
  }
  private async directory(root: string, id: string): Promise<string> {
    const candidate = path.join(root, id);
    const stat = await fs.lstat(candidate);
    const real = await fs.realpath(candidate);
    if (!stat.isDirectory() || stat.isSymbolicLink() || path.dirname(real) !== root) throw new Error('Artifact must be a direct directory inside the library.');
    return real;
  }
  configure(candidate: string): Promise<ArtifactLibrary> {
    return this.serial(async () => {
      if (!path.isAbsolute(candidate)) throw new Error('Choose an absolute artifact folder path.');
      await fs.mkdir(candidate, { recursive: true });
      const root = await fs.realpath(candidate);
      const entries = await fs.readdir(root);
      if (!entries.includes(manifestName)) {
        if (!(await emptyArtifactDirectory(root))) throw new Error('Choose an empty folder for a new artifact library, or an existing skillz artifact library.');
        await git(root, 'init');
        await fs.writeFile(path.join(root, '.gitignore'), '.artifact-contexts/\n.DS_Store\n', 'utf8');
        await writeJson(path.join(root, manifestName), { version: 1, artifacts: [] });
        await git(root, 'add', '--', manifestName, '.gitignore');
        await git(root, ...author, 'commit', '-m', 'Initialize artifact library');
      } else {
        const actual = await git(root, 'rev-parse', '--show-toplevel');
        if (await fs.realpath(actual) !== root) throw new Error('Artifact library must have its own Git repository.');
      }
      await writeJson(this.settingsFile, { root });
      return this.library();
    });
  }
  create(raw: CreateArtifact): Promise<ArtifactRecord> { return this.createFrom(raw, this.template, 'Scaffold artifact'); }
  async prebuilts(): Promise<PrebuiltArtifact[]> {
    return [
      { id: 'server-manager', title: 'Server Manager', description: 'Detect, launch, stop, and inspect explicitly whitelisted repository scripts and their listening ports.', requiresWriteAccess: false, requiresProcessProxy: true },
      { id: 'repo-issue-manager', title: 'Repository issue manager', description: 'Review, create, activate, close, and reopen Skillz issues in any repository you explicitly share.', requiresWriteAccess: true },
    ];
  }
  async installPrebuilt(id: string, access: ArtifactAccess, runtime?: import('../../shared/artifacts').CreateArtifact['runtime']): Promise<ArtifactRecord> {
    const preset = (await this.prebuilts()).find(item => item.id === artifactId.parse(id));
    if (!preset) throw new Error('Unknown prebuilt artifact.');
    const source = path.join(this.prebuiltHome, id);
    const available = await fs.lstat(source).then(stat => stat.isDirectory() && !stat.isSymbolicLink()).catch(() => false);
    if (!available) throw new Error(`The bundled ${preset.title} artifact is unavailable. Initialize its prebuilt artifact submodule.`);
    const checked = artifactAccessSchema.parse(access);
    if (preset.requiresWriteAccess && !checked.directories.some(directory => directory.access === 'write')) throw new Error('Share at least one repository with Allow changes enabled.');
    if (preset.requiresProcessProxy && !checked.directories.some(directory => directory.allowProcessProxy)) throw new Error('Share at least one repository with Allow Process Proxy enabled.');
    return this.createFrom({ title: preset.title, prompt: preset.description, sourceRoot: '', shareFacts: false, shareMemory: false, runtime, access: checked }, source, 'Install prebuilt artifact');
  }
  private createFrom(raw: CreateArtifact, source: string, initialCommit: string): Promise<ArtifactRecord> {
    return this.serial(async () => {
      const options = createArtifactSchema.parse(raw);
      const library = await this.library();
      if (!library.root) throw new Error('Configure an artifact folder first.');
      if ((options.shareFacts || options.shareMemory) && !options.sourceRoot) throw new Error('Open a source workspace before sharing context.');
      if (options.sourceRoot) options.sourceRoot = await fs.realpath(options.sourceRoot);
      const slug = options.title.normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 42) || 'artifact';
      const id = `${slug}-${randomUUID().slice(0, 8)}`;
      const root = path.join(library.root, id);
      await fs.mkdir(root).catch((error: NodeJS.ErrnoException) => { if (error.code !== 'EEXIST') throw error; });
      const destination = await fs.lstat(root);
      if (!destination.isDirectory() || destination.isSymbolicLink() || !(await emptyArtifactDirectory(root))) {
        throw new Error(`Artifact destination must be an empty directory: ${root}`);
      }
      // Preserve incomplete scaffolds on failure; never delete user files to retry.
      // Newer Node runtimes reject an existing directory with errorOnExist, even when empty.
      // Copy each entry into the reserved directory, retaining exclusive creation for every entry.
      for (const entry of await fs.readdir(source)) {
        if (entry === ".git") continue;
        await fs.cp(path.join(source, entry), path.join(root, entry), { recursive: true, errorOnExist: true, force: false, filter: (entry) => !['node_modules', '.DS_Store'].includes(path.basename(entry)) && (source !== this.template || path.basename(entry) !== 'package-lock.json') });
      }
      const app = artifactAppSchema.parse(options.app || defaultArtifactApp());
      await this.writeAppForRoot(root, app);
      const createdAt = new Date().toISOString();
      await writeJson(path.join(root, 'artifact.json'), { version: 1, id, title: options.title, prompt: options.prompt, createdAt });
      let record: ArtifactRecord = { ...options, app, id, root, createdAt, contextMode: 'none' };
      record = await this.linkContext(record);
      const { access, app: _app, ...localRecord } = record;
      await writeJson(path.join(root, '.artifact-local.json'), localRecord);
      const records = await this.readLocalRecords(); records[root] = localRecord;
      await writeJson(path.join(path.dirname(this.settingsFile), 'artifact-records.json'), records);
      if (access) await this.writePermissions(root, access);
      await git(root, 'init');
      await git(root, 'add', '--', '.');
      await git(root, ...author, 'commit', '-m', initialCommit);
      // Existing child repository is registered as a gitlink, not added as ordinary files.
      await git(library.root, '-c', 'protocol.file.allow=always', 'submodule', 'add', `./${id}`, id);
      await git(library.root, 'submodule', 'absorbgitdirs', '--', id);
      await writeJson(path.join(library.root, manifestName), { version: 1, artifacts: [...library.artifacts.map((item) => item.id), id] });
      await git(library.root, 'add', '--', manifestName, '.gitmodules', id);
      await git(library.root, ...author, 'commit', '-m', `Add artifact: ${options.title}`, '--', manifestName, '.gitmodules', id);
      return record;
    });
  }
  private async linkContext(record: ArtifactRecord): Promise<ArtifactRecord> {
    // Shared snapshots are mounted read-only at /context; never link host paths into writable repositories.
    await fs.mkdir(path.join(record.root, '.context'), { recursive: true });
    return { ...record, contextMode: record.shareFacts || record.shareMemory ? 'snapshot' : 'none' };
  }
  private async readLocalRecords(): Promise<Record<string, Partial<ArtifactRecord>>> {
    try { return await readJson(path.join(path.dirname(this.settingsFile), 'artifact-records.json')) as Record<string, Partial<ArtifactRecord>>; }
    catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return {}; throw error; }
  }
  async contextDirectory(id: string): Promise<string> {
    const record = await this.find(id);
    const directory = path.join(this.contextHome, id);
    await fs.mkdir(directory, { recursive: true });
    await this.syncContext(record);
    return directory;
  }
  private contextNames(record: ArtifactRecord): string[] { return [...(record.shareFacts ? ['repo_facts.md'] : []), ...(record.shareMemory ? ['memory_observability.md'] : [])]; }
  async syncContext(record: ArtifactRecord): Promise<void> {
    if (record.contextMode !== 'snapshot') return;
    const exportPath = path.join(this.contextHome, record.id);
    await fs.mkdir(exportPath, { recursive: true });
    for (const name of this.contextNames(record)) {
      const target = path.join(exportPath, name);
      try { const source = path.join(record.sourceRoot, name); const stat = await fs.lstat(source); if (!stat.isFile() || stat.size > 5000000) { await fs.rm(target, { force: true }); continue; } await fs.copyFile(source, target); }
      catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') await fs.rm(target, { force: true }); else throw error; }
    }
  }
  private async readPermissions(): Promise<Record<string, ArtifactAccess>> {
    try {
      const data = await readJson(path.join(path.dirname(this.settingsFile), 'artifact-permissions.json'));
      if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('Invalid artifact folder permissions.');
      return data as Record<string, ArtifactAccess>;
    } catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return {}; throw error; }
  }
  private async writePermissions(root: string, access: ArtifactAccess): Promise<void> {
    const checked = artifactAccessSchema.parse(access);
    const paths = new Set<string>();
    for (const directory of checked.directories) {
      if (!path.isAbsolute(directory.path)) throw new Error('Allowed folders must use absolute paths.');
      directory.path = await fs.realpath(directory.path);
      if (!(await fs.stat(directory.path)).isDirectory()) throw new Error('Choose a directory for artifact access.');
      const key = process.platform === 'win32' ? directory.path.toLocaleLowerCase() : directory.path;
      if (paths.has(key)) throw new Error('Each shared folder path may be granted only once.');
      paths.add(key);
    }
    const data = await this.readPermissions();
    data[root] = checked;
    // Keep grants in desktop settings, outside the generated repository and its Git history.
    await writeJson(path.join(path.dirname(this.settingsFile), 'artifact-permissions.json'), data);
  }
  async access(id: string): Promise<ArtifactAccess> { return (await this.find(id)).access || { directories: [], allowWorkspaceRead: false }; }
  saveAccess(id: string, access: ArtifactAccess): Promise<void> { return this.serial(async () => { const record = await this.find(id); await this.writePermissions(record.root, access); }); }
  async apis(id: string): Promise<ArtifactApis> { const record = await this.find(id); return artifactApisSchema.parse(await readJson(path.join(record.root, '.artifact/apis.json'))); }
  async saveApis(id: string, config: ArtifactApis): Promise<void> {
    const validated = artifactApisSchema.parse(config);
    if (JSON.stringify(validated).length > 256000) throw new Error('API configuration exceeds 256 KB.');
    const record = await this.find(id);
    const directory = path.join(record.root, '.artifact');
    if ((await fs.lstat(directory)).isSymbolicLink()) throw new Error('API configuration directory cannot be a symlink.');
    await writeJson(path.join(directory, 'apis.json'), validated);
  }
  private async appForRoot(root: string): Promise<ArtifactAppConfig> {
    try { return artifactAppSchema.parse(await readJson(path.join(root, '.artifact/app.json'))); }
    catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return defaultArtifactApp(); throw error; }
  }
  private async writeAppForRoot(root: string, config: ArtifactAppConfig): Promise<void> {
    const validated = artifactAppSchema.parse(config);
    await writeJson(path.join(root, '.artifact/app.json'), validated);
    const packageFile = path.join(root, 'package.json');
    const manifest = await readJson(packageFile) as { dependencies?: Record<string, string>; [key: string]: unknown };
    const dependencies = { ...(manifest.dependencies || {}) };
    const drivers: Record<ArtifactAppConfig['database']['dialect'], [string, string]> = {
      sqlite: ['sqlite3', '^5.1.7'], postgres: ['pg', '^8.16.3'], mysql: ['mysql2', '^3.14.4'], mariadb: ['mariadb', '^3.4.5'], mssql: ['tedious', '^19.1.3'],
    };
    for (const [name] of Object.values(drivers)) delete dependencies[name];
    if (validated.database.enabled) {
      dependencies.sequelize = '^6.37.7';
      const [name, version] = drivers[validated.database.dialect]; dependencies[name] = version;
    } else delete dependencies.sequelize;
    await writeJson(packageFile, { ...manifest, dependencies });
  }
  private async appRuntimeReady(root: string): Promise<boolean> {
    const required = ['.artifact/runtime.json', 'server/agent.ts', 'server/database.ts', 'src/skillz.ts'];
    const filesReady = await Promise.all(required.map(name => fs.lstat(path.join(root, name)).then(stat => stat.isFile() && !stat.isSymbolicLink(), () => false)));
    const runtimeReady = await readJson(path.join(root, '.artifact/runtime.json')).then(value => Boolean(value && typeof value === 'object' && (value as { version?: unknown }).version === 1), () => false);
    const gatewayReady = await fs.readFile(path.join(root, 'server/gateway.ts'), 'utf8').then(text => text.includes('advertisedCommands'), () => false);
    const indexReady = await fs.readFile(path.join(root, 'server/index.ts'), 'utf8').then(text => text.includes('attachArtifactAgent') && text.includes('closeDatabase'), () => false);
    return filesReady.every(Boolean) && runtimeReady && gatewayReady && indexReady;
  }
  private async commitPaths(root: string, message: string, files: string[]): Promise<string | undefined> {
    if (!(await git(root, 'status', '--porcelain', '--untracked-files=all', '--', ...files))) return undefined;
    await git(root, 'add', '-A', '--', ...files);
    await git(root, ...author, 'commit', '--only', '-m', message, '--', ...files);
    return git(root, 'rev-parse', 'HEAD');
  }
  private async migrateAppRuntime(root: string, config: ArtifactAppConfig): Promise<ArtifactAppSaveResult> {
    if (await this.appRuntimeReady(root)) return { migrated: false };
    const managed = ['.artifact/runtime.json', 'server/agent.ts', 'server/database.ts', 'server/gateway.ts', 'server/schema.ts', 'src/skillz.ts'];
    const replacements = new Map<string, string>();
    for (const relative of managed) {
      const source = path.join(this.template, relative);
      const sourceStat = await fs.lstat(source);
      if (!sourceStat.isFile() || sourceStat.isSymbolicLink()) throw new Error(`Artifact template runtime file is invalid: ${relative}`);
      replacements.set(relative, await fs.readFile(source, 'utf8'));
    }
    const indexRelative = 'server/index.ts';
    const indexFile = path.join(root, indexRelative);
    let index = await fs.readFile(indexFile, 'utf8').catch((error: NodeJS.ErrnoException) => {
      if (error.code === 'ENOENT') return '';
      throw error;
    });
    if (!index) index = await fs.readFile(path.join(this.template, indexRelative), 'utf8');
    else {
      if (!index.includes("from './agent'")) {
        const updated = index.replace(/(import\s+\{\s*attachGateway\s*\}\s+from\s+['"]\.\/gateway['"];?)/, "$1\nimport { attachArtifactAgent } from './agent';\nimport { closeDatabase } from './database';");
        if (updated === index) throw new Error('The artifact server entry point was customized beyond automatic migration. Restore server/index.ts or ask the artifact agent to merge the current template runtime.');
        index = updated;
      } else if (!index.includes("from './database'")) {
        index = index.replace(/(import\s+\{\s*attachArtifactAgent\s*\}\s+from\s+['"]\.\/agent['"];?)/, "$1\nimport { closeDatabase } from './database';");
      }
      if (!index.includes('const closeAgent = attachArtifactAgent(app);')) {
        const updated = index.replace(/(const\s+closeGateway\s*=\s*attachGateway\(app,\s*server,\s*root\);)/, '$1\nconst closeAgent = attachArtifactAgent(app);');
        if (updated === index) throw new Error('The artifact server startup was customized beyond automatic migration. Restore server/index.ts or ask the artifact agent to merge the current template runtime.');
        index = updated;
      }
      if (!index.includes('closeAgent();')) index = index.replace(/(async\s+function\s+stop\(\)\s*\{)/, '$1 closeAgent();');
      if (!index.includes('await closeDatabase();')) index = index.replace(/(async\s+function\s+stop\(\)\s*\{[^}]*?closeGateway\(\);)/, '$1 await closeDatabase();');
      if (!index.includes('closeAgent();') || !index.includes('await closeDatabase();')) throw new Error('The artifact server shutdown handler was customized beyond automatic migration. Restore server/index.ts or ask the artifact agent to merge the current template runtime.');
    }
    replacements.set(indexRelative, index);
    const changed: string[] = [];
    for (const [relative, next] of replacements) {
      const target = path.join(root, relative);
      const stat = await fs.lstat(target).catch((error: NodeJS.ErrnoException) => {
        if (error.code === 'ENOENT') return undefined;
        throw error;
      });
      if (stat?.isSymbolicLink() || (stat && !stat.isFile())) throw new Error(`Artifact runtime path must be a regular file: ${relative}`);
      const previous = stat ? await fs.readFile(target, 'utf8') : undefined;
      if (previous !== next) changed.push(relative);
    }
    const commitScope = [...managed, indexRelative, '.gitignore', '.artifact/app.json', 'package.json'];
    const checkpointCommit = await this.commitPaths(root, 'Checkpoint artifact before runtime migration', commitScope);
    for (const relative of changed) await writeText(path.join(root, relative), replacements.get(relative)!);
    const ignoreFile = path.join(root, '.gitignore');
    const ignore = await fs.readFile(ignoreFile, 'utf8').catch((error: NodeJS.ErrnoException) => { if (error.code === 'ENOENT') return ''; throw error; });
    if (!ignore.split(/\r?\n/).includes('.artifact-data/')) await writeText(ignoreFile, `${ignore}${ignore && !ignore.endsWith('\n') ? '\n' : ''}.artifact-data/\n`);
    await this.writeAppForRoot(root, config);
    const commit = await this.commitPaths(root, 'Upgrade artifact app runtime to v1', commitScope);
    if (!commit) throw new Error('Artifact runtime migration produced no Git commit.');
    return { migrated: true, commit, ...(checkpointCommit ? { checkpointCommit } : {}) };
  }
  async app(id: string): Promise<ArtifactAppConfig> { return this.appForRoot((await this.find(id)).root); }
  saveApp(id: string, config: ArtifactAppConfig): Promise<ArtifactAppSaveResult> { return this.serial(async () => {
    const root = (await this.find(id)).root; const validated = artifactAppSchema.parse(config);
    const result = validated.chatbot.enabled || validated.database.enabled ? await this.migrateAppRuntime(root, validated) : { migrated: false };
    if (!result.migrated) await this.writeAppForRoot(root, validated);
    return result;
  }); }
}
