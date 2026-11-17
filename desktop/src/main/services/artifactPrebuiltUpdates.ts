import { randomUUID } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import type { ArtifactRecord, PrebuiltUpdateFile, PrebuiltUpdateRequest, PrebuiltUpdateResult, PrebuiltUpdateReview, PrebuiltUpdateStatus } from '../../shared/artifacts';
import type { ArtifactLibraryService } from './artifactLibrary';
import { checkedSourcePath, mergeSourceFile, originalPrebuiltSource, prebuiltBaselineFile, preserveAppDependencies, readBundledSource, readInstalledSource, readSourceSnapshot, replaceSourceFile, sameSource, sourceHash, writeSourceSnapshot, type SourceFile, type SourceSnapshot } from './artifactPrebuiltSource';

interface UpdatePlan { record: ArtifactRecord; base: SourceSnapshot; incoming: SourceSnapshot; local: SourceSnapshot; review: PrebuiltUpdateReview; results: SourceSnapshot }
function preview(file?: SourceFile): { text?: string; binary: boolean; truncated: boolean } {
  if (!file) return { binary: false, truncated: false };
  const bytes = Buffer.from(file.data, 'base64');
  const text = bytes.toString('utf8');
  const binary = bytes.includes(0) || !Buffer.from(text).equals(bytes);
  return { text: binary ? undefined : text.slice(0, 12000), binary, truncated: !binary && text.length > 12000 };
}
function installedVersionMatches(relative: string, local?: SourceFile, incoming?: SourceFile): boolean {
  return sameSource(local, incoming && relative === 'package.json' ? preserveAppDependencies(incoming, local) : incoming);
}
export class ArtifactPrebuiltUpdates {
  private queue: Promise<unknown> = Promise.resolve();
  private serial<T>(operation: () => Promise<T>): Promise<T> { const pending = this.queue.then(operation); this.queue = pending.catch(() => {}); return pending; }
  constructor(private readonly library: ArtifactLibraryService, private readonly home: string) {}
  private baselineFile(record: ArtifactRecord): string { return prebuiltBaselineFile(this.home, record.root); }
  private saveBaseline(record: ArtifactRecord, files: SourceSnapshot): Promise<void> {
    return writeSourceSnapshot(this.baselineFile(record), { version: 1, root: record.root, prebuiltId: record.prebuiltId, files });
  }
  private async baseline(record: ArtifactRecord): Promise<SourceSnapshot> {
    try {
      const stored = await readSourceSnapshot(this.baselineFile(record));
      if (stored.root !== record.root || stored.prebuiltId !== record.prebuiltId) throw new Error('The saved prebuilt baseline belongs to another artifact.');
      return stored.files;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
      const files = await originalPrebuiltSource(record.root);
      await this.saveBaseline(record, files);
      return files;
    }
  }
  private async sources(id: string) {
    const record = await this.library.find(id);
    if (!record.prebuiltId) throw new Error('Source updates are available only for installed prebuilts.');
    const source = await this.library.prebuiltSource(record.prebuiltId);
    const incoming = await readBundledSource(source);
    const base = await this.baseline(record);
    const paths = [...new Set([...Object.keys(base), ...Object.keys(incoming)])];
    const local = await readInstalledSource(record.root, paths);
    const changed = paths.filter(relative => !sameSource(base[relative], incoming[relative])).sort();
    return { record, incoming, base, local, changed };
  }
  status(id: string): Promise<PrebuiltUpdateStatus> { return this.serial(() => this.checkStatus(id)); }
  private async checkStatus(id: string): Promise<PrebuiltUpdateStatus> {
    const record = await this.library.find(id);
    if (!record.prebuiltId) return { available: false, changedFiles: 0 };
    try {
      const source = await this.sources(id);
      // A manually refreshed copy can safely acknowledge changes already installed.
      const pending = source.changed.filter(relative => !installedVersionMatches(relative, source.local[relative], source.incoming[relative]));
      if (source.changed.length && !pending.length) {
        await this.saveBaseline(source.record, source.incoming);
        return { available: false, changedFiles: 0 };
      }
      return { available: pending.length > 0, changedFiles: pending.length };
    } catch (error) { return { available: false, changedFiles: 0, error: String(error).replace(/^Error: /, '') }; }
  }
  private async plan(id: string): Promise<UpdatePlan> {
    const source = await this.sources(id);
    const results: SourceSnapshot = {};
    const files: PrebuiltUpdateFile[] = [];
    for (const relative of source.changed) {
      const base = source.base[relative], local = source.local[relative], incoming = source.incoming[relative];
      let action: PrebuiltUpdateFile['action'], result: SourceFile | undefined;
      if (installedVersionMatches(relative, local, incoming)) continue;
      else if (sameSource(local, base)) { action = 'replace'; result = incoming; }
      else if (base && local && incoming) {
        result = await mergeSourceFile(base, local, incoming);
        action = result ? (sameSource(result, local) ? 'keep' : 'merge') : 'conflict';
      } else action = 'conflict';
      if (result && relative === 'package.json') result = preserveAppDependencies(result, local);
      if (result) results[relative] = result;
      const before = preview(local), after = preview(incoming), merged = preview(result);
      files.push({ path: relative, kind: !base ? 'added' : !incoming ? 'removed' : 'changed', action, installed: before.text, incoming: after.text, result: merged.text, binary: before.binary || after.binary || merged.binary, truncated: before.truncated || after.truncated || merged.truncated });
    }
    const token = sourceHash({ baseline: { data: sourceHash(source.base), executable: false }, incoming: { data: sourceHash(source.incoming), executable: false }, local: { data: sourceHash(source.local), executable: false } });
    return { ...source, review: { token, files }, results };
  }
  review(id: string): Promise<PrebuiltUpdateReview> { return this.serial(async () => (await this.plan(id)).review); }
  apply(id: string, request: PrebuiltUpdateRequest): Promise<PrebuiltUpdateResult> { return this.serial(() => this.applyReview(id, request)); }
  private async applyReview(id: string, request: PrebuiltUpdateRequest): Promise<PrebuiltUpdateResult> {
    const plan = await this.plan(id);
    if (request.token !== plan.review.token) throw new Error('The installed or bundled source changed after this review. Review the update again.');
    for (const relative of Object.keys(request.resolutions)) if (!plan.review.files.some(file => file.path === relative && file.action === 'conflict')) throw new Error('Choose a resolution only for a listed conflict.');
    const changes: { path: string; result?: SourceFile }[] = [];
    for (const file of plan.review.files) {
      let result = plan.results[file.path];
      if (file.action === 'conflict') {
        const choice = request.resolutions[file.path];
        if (!choice) throw new Error(`Choose which version to keep for ${file.path}.`);
        result = choice === 'installed' ? plan.local[file.path] : plan.incoming[file.path];
      }
      if (result && file.path === 'package.json') result = preserveAppDependencies(result, plan.local[file.path]);
      if (!sameSource(plan.local[file.path], result)) changes.push({ path: file.path, result });
    }
    // Validate every destination before writing any file, including deleted files.
    for (const change of changes) await checkedSourcePath(plan.record.root, change.path);
    const original = await readInstalledSource(plan.record.root, changes.map(change => change.path), false);
    const backup = path.join(path.dirname(this.baselineFile(plan.record)), 'backups', randomUUID() + '.json');
    await writeSourceSnapshot(backup, { version: 1, root: plan.record.root, prebuiltId: plan.record.prebuiltId, createdAt: new Date().toISOString(), files: original, missing: changes.filter(change => !original[change.path]).map(change => change.path), baseline: plan.base });
    const latest = await this.sources(id);
    if (sourceHash(latest.base) !== sourceHash(plan.base) || sourceHash(latest.local) !== sourceHash(plan.local) || sourceHash(latest.incoming) !== sourceHash(plan.incoming)) throw new Error('Source changed while preparing the update. Review the update again.');
    const written: string[] = [];
    try {
      for (const change of changes) { await replaceSourceFile(plan.record.root, change.path, change.result); written.push(change.path); }
      await this.saveBaseline(plan.record, plan.incoming);
    } catch (error) {
      const failed: string[] = [];
      for (const relative of written.reverse()) { try { await replaceSourceFile(plan.record.root, relative, original[relative]); } catch { failed.push(relative); } }
      if (failed.length) throw new Error(`Update failed. Restore ${failed.join(', ')} from the retained backup: ${backup}. ${String(error)}`);
      throw error;
    }
    return { updatedFiles: changes.length, backup };
  }
}
