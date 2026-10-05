const { spawnSync } = require('node:child_process');
const { randomUUID } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const prebuilts = ['server-manager', 'repo-issue-manager'].map(id => ({
  id, path: `desktop/prebuilt-artifacts/${id}`, branch: `prebuilt/${id}`,
}));
const requiredFiles = ['artifact.json', 'package.json', 'server/index.ts', 'src/App.tsx'];

function run(root, args, allowFailure = false) {
  const env = { ...process.env, GIT_TERMINAL_PROMPT: '0' };
  for (const key of ['GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES']) delete env[key];
  const result = spawnSync('git', args, { cwd: root, env, windowsHide: true, encoding: 'utf8', timeout: 120000, maxBuffer: 8 * 1024 * 1024 });
  if (result.error) throw result.error;
  if (result.status !== 0 && !allowFailure) throw new Error((result.stderr || result.stdout || `git ${args[0]} exited with ${result.status}`).trim());
  return { status: result.status, stdout: result.stdout.trim(), stderr: result.stderr.trim() };
}
function git(root, ...args) { return run(root, args).stdout; }

function configuration(root, revision) {
  const source = revision ? ['--blob', `${revision}:.gitmodules`] : ['--file', '.gitmodules'];
  for (const item of prebuilts) {
    for (const [key, value] of Object.entries({ path: item.path, url: './', branch: item.branch })) {
      const actual = run(root, ['config', ...source, '--get', `submodule.${item.path}.${key}`], true);
      if (actual.status !== 0 || actual.stdout !== value) {
        throw new Error(`${revision ? 'Committed' : 'Working'} .gitmodules must set ${item.path}.${key} to ${value}. Commit the relative URLs before publishing.`);
      }
    }
  }
}

function check(root) {
  configuration(root);
  for (const item of prebuilts) {
    const child = path.join(root, item.path);
    try {
      const directory = fs.lstatSync(child);
      if (!directory.isDirectory() || directory.isSymbolicLink()) throw new Error('Missing or linked directory');
      if (fs.realpathSync(child) !== fs.realpathSync(git(child, 'rev-parse', '--show-toplevel'))) throw new Error('Not an independent repository');
      for (const file of requiredFiles) {
        const stat = fs.lstatSync(path.join(child, file));
        if (!stat.isFile() || stat.isSymbolicLink()) throw new Error(`Missing ${file}`);
      }
    } catch (error) {
      throw new Error(`Prebuilt ${item.id} is unavailable: ${error.message}. Run npm --prefix desktop run submodules:init before developing or building.`);
    }
  }
}

function snapshot(root) {
  const head = git(root, 'rev-parse', 'HEAD');
  configuration(root, head);
  const modules = prebuilts.map(item => {
    const entry = git(root, 'ls-tree', head, '--', item.path);
    const match = /^160000 commit ([a-f0-9]{40,64})\t/.exec(entry);
    if (!match) throw new Error(`The committed application must contain the ${item.id} submodule pointer.`);
    return { ...item, pin: match[1] };
  });
  return { head, modules };
}

function destination(root, remote) {
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._/-]*$/.test(remote)) throw new Error('Choose a configured Git remote name, such as origin.');
  const urls = git(root, 'remote', 'get-url', '--push', '--all', remote).split('\n').filter(Boolean);
  if (urls.length !== 1) throw new Error(`Remote ${remote} must have exactly one push URL so prebuilts and application have the same destination.`);
  return urls[0];
}

function withRefs(root, operation) {
  const prefix = `refs/skillz-prebuilt-check/${randomUUID()}`;
  const refs = [];
  const reserve = suffix => { const ref = `${prefix}/${suffix}`; refs.push(ref); return ref; };
  try { return operation(reserve); }
  finally { for (const ref of refs) git(root, 'update-ref', '-d', ref); }
}

function remoteTips(root, url, modules, reserve) {
  const advertised = git(root, 'ls-remote', '--refs', '--', url, ...modules.map(item => `refs/heads/${item.branch}`));
  const branches = new Set(advertised.split('\n').filter(Boolean).map(line => line.split('\t')[1]));
  const tips = new Map();
  for (const item of modules) {
    const branch = `refs/heads/${item.branch}`;
    if (!branches.has(branch)) continue;
    const ref = reserve(`remote/${item.id}`);
    // Fetch in the parent repository to retain its configured credential helpers
    // and HTTP headers, including authenticated private-repository CI checkouts.
    git(root, 'fetch', '--no-tags', '--no-write-fetch-head', '--no-recurse-submodules', '--', url, `${branch}:${ref}`);
    tips.set(item.id, git(root, 'rev-parse', ref));
  }
  return tips;
}

function ancestor(root, older, newer) {
  const result = run(root, ['merge-base', '--is-ancestor', older, newer], true);
  if (result.status !== 0 && result.status !== 1) throw new Error(result.stderr || 'Cannot check prebuilt ancestry.');
  return result.status === 0;
}

function verifyPins(root, url, modules) {
  withRefs(root, reserve => {
    const tips = remoteTips(root, url, modules, reserve);
    for (const item of modules) {
      const tip = tips.get(item.id);
      if (!tip) throw new Error(`Destination is missing ${item.branch}. Run npm --prefix desktop run repo:publish -- <remote>.`);
      const exists = run(root, ['cat-file', '-e', `${item.pin}^{commit}`], true).status === 0;
      if (!exists || !ancestor(root, item.pin, tip)) {
        throw new Error(`Destination ${item.branch} does not contain the pinned ${item.id} commit ${item.pin}. Publish its history before the application.`);
      }
    }
  });
}

function publication(root) {
  check(root);
  const state = snapshot(root);
  const branch = run(root, ['symbolic-ref', '--quiet', '--short', 'HEAD'], true).stdout;
  if (!branch) throw new Error('Switch the application to a branch before publishing.');
  if (branch.startsWith('prebuilt/')) throw new Error('Publish from an application branch, not a prebuilt source branch.');
  const changed = git(root, 'status', '--porcelain=v1', '--untracked-files=all', '--', '.gitmodules', ...prebuilts.map(item => item.path));
  if (changed) throw new Error('Commit changes inside the prebuilts, then commit .gitmodules and the updated submodule pointers in the application before publishing.');
  for (const item of state.modules) {
    const child = path.join(root, item.path);
    if (git(child, 'status', '--porcelain=v1', '--untracked-files=all')) throw new Error(`Commit changes inside ${item.id} before publishing.`);
    if (git(child, 'rev-parse', 'HEAD') !== item.pin) throw new Error(`Commit the updated ${item.id} pointer in the application before publishing.`);
  }
  return { ...state, branch };
}

function publish(root, remote = 'origin', log = console.log) {
  const state = publication(root);
  const url = destination(root, remote);
  withRefs(root, reserve => {
    // The child object databases are independent, even though their destination
    // is the same repository. Import only the committed pins into temporary refs.
    for (const item of state.modules) {
      git(root, '-c', 'protocol.file.allow=always', 'fetch', '--no-tags', '--no-write-fetch-head', '--no-recurse-submodules', '--', path.join(root, item.path), `${item.pin}:${reserve(`local/${item.id}`)}`);
    }
    const tips = remoteTips(root, url, state.modules, reserve);
    const updates = [];
    for (const item of state.modules) {
      const tip = tips.get(item.id);
      if (tip && ancestor(root, item.pin, tip)) continue; // Preserve an already-newer destination branch.
      if (tip && !ancestor(root, tip, item.pin)) throw new Error(`Destination ${item.branch} has diverged. Fetch and reconcile its history before publishing; no force push is performed.`);
      updates.push(`${item.pin}:refs/heads/${item.branch}`);
    }
    if (updates.length) {
      log(`Publishing prebuilt histories to ${remote}…`);
      git(root, 'push', '--atomic', '--no-recurse-submodules', '--', url, ...updates);
    }
    // Fetch again instead of trusting cached tracking refs or original remotes.
    verifyPins(root, url, state.modules);
    const current = publication(root);
    if (current.head !== state.head || current.branch !== state.branch || destination(root, remote) !== url) {
      throw new Error('The application or destination changed during publication. Run the publish command again.');
    }
    log(`Prebuilt pins verified on ${remote}; publishing ${state.branch}…`);
    // Send the captured commit to the captured URL, not mutable branch/remote
    // names that could change between verification and the push.
    git(root, 'push', '--no-recurse-submodules', '--', url, `${state.head}:refs/heads/${state.branch}`);
    git(root, 'update-ref', `refs/remotes/${remote}/${state.branch}`, state.head);
    git(root, 'config', '--local', `branch.${state.branch}.remote`, remote);
    git(root, 'config', '--local', `branch.${state.branch}.merge`, `refs/heads/${state.branch}`);
  });
}

function main(args = process.argv.slice(2), root = path.resolve(__dirname, '../..')) {
  const [operation, remote = 'origin', ...extra] = args;
  if (extra.length || (!['verify', 'publish'].includes(operation) && args.length > 1)) throw new Error('Usage: prebuilt-artifacts.cjs <init|update|check|verify|publish> [remote]');
  if (operation === 'init' || operation === 'update') {
    configuration(root);
    git(root, 'submodule', 'sync', '--recursive');
    git(root, 'submodule', 'update', '--init', ...(operation === 'update' ? ['--remote'] : []), '--recursive');
    check(root);
  } else if (operation === 'check') check(root);
  else if (operation === 'verify') verifyPins(root, destination(root, remote), snapshot(root).modules);
  else if (operation === 'publish') publish(root, remote);
  else throw new Error('Usage: prebuilt-artifacts.cjs <init|update|check|verify|publish> [remote]');
  console.log(`Prebuilt ${operation} complete.`);
}

module.exports = { check, main, publish, snapshot, verifyPins, destination };
if (require.main === module) {
  try { main(); }
  catch (error) { console.error(`Prebuilt workflow failed: ${error.message}`); process.exitCode = 1; }
}
