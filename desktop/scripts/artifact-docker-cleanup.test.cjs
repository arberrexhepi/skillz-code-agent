const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const load = require('./load-ts.cjs');
const { artifactDependencyVolume, cleanArtifactDockerResources, planArtifactDockerCleanup } = load(() => ({
  ...require('../src/main/services/artifactSandbox.ts'),
  ...require('../src/main/services/artifactDockerCleanup.ts'),
}));

function fixture(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'skillz cleanup ')));
  t.after(() => fs.rmSync(root, { recursive: true, force: true, maxRetries: 5 }));
  const current = 'skillz-artifact:' + '1'.repeat(20);
  const obsolete = 'skillz-artifact:' + '2'.repeat(20);
  const attachedImage = 'skillz-artifact:' + '3'.repeat(20);
  const installedVolume = artifactDependencyVolume(root);
  const orphanedVolume = 'skillz-artifact-deps-' + '4'.repeat(20);
  const attachedVolume = 'skillz-artifact-deps-' + '5'.repeat(20);
  const calls = [];
  const tools = {
    dockerCommand: async () => 'fixture-docker',
    artifactRuntimeImage: async () => current,
    command: async (_docker, args) => {
      calls.push(args);
      const key = args.join(' ');
      if (key.startsWith('image ls ')) return [current, obsolete, attachedImage, 'skillz-artifact:latest', 'other:tag'].join('\n');
      if (key.startsWith('ps --all ')) return attachedImage;
      if (key === 'volume ls --format {{.Name}}') return [installedVolume, orphanedVolume, attachedVolume, 'skillz-artifact-deps-latest', 'other-volume'].join('\n');
      if (key === 'volume ls --filter dangling=false --format {{.Name}}') return attachedVolume;
      return '';
    },
  };
  return { root, current, obsolete, attachedImage, installedVolume, orphanedVolume, attachedVolume, calls, tools };
}

test('cleanup planning preserves current, installed, and attached Docker resources', async (t) => {
  const value = fixture(t);
  const plan = await planArtifactDockerCleanup([value.root], value.root, value.tools);
  assert.equal(plan.currentImage, value.current);
  assert.deepEqual(plan.obsoleteImages, [value.obsolete]);
  assert.deepEqual(plan.orphanedVolumes, [value.orphanedVolume]);
  assert.deepEqual(plan.preservedImages, [value.current, value.attachedImage].sort());
  assert.deepEqual(plan.preservedVolumes, [value.installedVolume, value.attachedVolume].sort());
  assert.equal(JSON.stringify(plan).includes('latest'), false);
  assert.equal(JSON.stringify(plan).includes('other'), false);
});

test('cleanup removes only the reviewed resources and reports a Docker race without forcing it', async (t) => {
  const value = fixture(t);
  const base = value.tools.command;
  value.tools.command = async (docker, args, cwd) => {
    if (args[0] === 'image' && args[1] === 'rm') throw new Error('image is being used by a container');
    return base(docker, args, cwd);
  };
  const result = await cleanArtifactDockerResources([value.root], value.root, value.tools);
  assert.deepEqual(result.removedVolumes, [value.orphanedVolume]);
  assert.deepEqual(result.removedImages, []);
  assert.equal(result.failures.length, 1);
  assert.match(result.failures[0], /being used by a container/);
  assert.equal(value.calls.some(args => args.join(' ') === `volume rm ${value.installedVolume}`), false);
  assert.equal(value.calls.some(args => args.join(' ') === `volume rm ${value.attachedVolume}`), false);
});
