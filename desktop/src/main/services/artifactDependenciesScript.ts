// Runs inside the Linux sandbox, against its dependency volume, never host node_modules.
export const artifactDependenciesScript = String.raw`
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { spawnSync } = require('node:child_process');
const root = process.cwd();
const localRequire = createRequire(path.join(root, 'package.json'));

function npm(args) {
  const child = spawnSync('/usr/local/bin/npm', args, { cwd: root, env: process.env, stdio: 'inherit' });
  if (child.error) throw child.error;
  if (child.status !== 0) throw new Error('Artifact dependency preparation failed. See the npm output above.');
}

function sqliteReady() {
  // A fresh process avoids caching a failed or replaced native module.
  return spawnSync(process.execPath, ['-e', 'require("sqlite3")'], { cwd: root, stdio: 'pipe' }).status === 0;
}

try {
  fs.mkdirSync(process.env.HOME, { recursive: true });
  npm(['install', '--ignore-scripts=false', '--nodedir=/usr/local', '--no-audit', '--no-fund', '--fetch-retries=0', '--fetch-timeout=30000']);
  let sqlite;
  try { sqlite = localRequire.resolve('sqlite3/package.json'); }
  catch (error) { if (error.code !== 'MODULE_NOT_FOUND') throw error; }
  if (sqlite && !sqliteReady()) {
    console.log('Repairing the SQLite native binding in the artifact dependency volume…');
    // Use the compiler and Node headers in the image: no host binary or header download.
    npm(['rebuild', 'sqlite3', '--ignore-scripts=false', '--build-from-source', '--nodedir=/usr/local', '--foreground-scripts', '--no-audit', '--no-fund']);
    if (!sqliteReady()) throw new Error('SQLite native binding is still unavailable after rebuilding. See the build output above and retry Start preview.');
    console.log('SQLite native binding repaired.');
  }
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
`;
