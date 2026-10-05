import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, isAbsolute, join, relative, resolve } from 'node:path';
import type { Plugin } from 'vite';

// Record packages whose source survives bundling, including devDependencies.
export function thirdPartyNoticesPlugin(target: string, projectDir: string): Plugin {
  return {
    name: `skillz-third-party-notices-${target}`,
    apply: 'build',
    writeBundle(options, bundle) {
      const packages = new Set<string>();
      const files: { path: string; sha256: string }[] = [];
      const outputDir = options.dir ? resolve(projectDir, options.dir) : dirname(resolve(projectDir, options.file!));
      for (const item of Object.values(bundle)) {
        const file = join(outputDir, item.fileName);
        files.push({ path: relative(projectDir, file).replaceAll('\\', '/'), sha256: createHash('sha256').update(readFileSync(file)).digest('hex') });
        if (item.type !== 'chunk') continue;
        for (const id of Object.keys(item.modules)) {
          const source = id.replace(/^\0/, '').split('?')[0];
          if (!source.replaceAll('\\', '/').includes('/node_modules/') || !isAbsolute(source) || !existsSync(source)) continue;
          const normalized = source.replaceAll('\\', '/');
          const marker = normalized.lastIndexOf('/node_modules/') + '/node_modules/'.length;
          const segments = normalized.slice(marker).split('/');
          const directory = normalized.slice(0, marker) + segments.slice(0, segments[0].startsWith('@') ? 2 : 1).join('/');
          const info = JSON.parse(readFileSync(join(directory, 'package.json'), 'utf8'));
          if (!info.name || !info.version) throw new Error(`Bundled dependency has no package identity: ${directory}`);
          const location = relative(projectDir, directory).replaceAll('\\', '/');
          if (!location.startsWith('node_modules/')) throw new Error(`Bundled dependency is outside desktop/node_modules: ${info.name}`);
          packages.add(location);
        }
      }
      const destination = join(projectDir, 'out', 'licenses');
      mkdirSync(destination, { recursive: true });
      writeFileSync(join(destination, `bundles-${target}.json`), JSON.stringify({ formatVersion: 1, target, packages: [...packages].sort(), files: files.sort((a, b) => a.path.localeCompare(b.path)) }, null, 2) + '\n');
    },
  };
}
