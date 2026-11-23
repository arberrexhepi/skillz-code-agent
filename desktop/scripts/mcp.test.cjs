const assert = require('node:assert/strict');
const { execFileSync, spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { once } = require('node:events');
const loadTypeScript = require('./load-ts.cjs');

const { AgentService, ArtifactAgentExecution, ArtifactLibraryService, ArtifactsService, ArtifactSandbox, McpHost, RuntimeSettingsService, removeArtifactDependencyVolumes } = loadTypeScript(() => ({
  ...require('../src/main/services/agent.ts'),
  ...require('../src/main/services/artifactAgent.ts'),
  ...require('../src/main/services/artifactLibrary.ts'),
  ...require('../src/main/services/artifacts.ts'),
  ...require('../src/main/services/artifactSandbox.ts'),
  ...require('../src/main/services/artifactDockerCleanup.ts'),
  ...require('../src/main/services/mcp.ts'),
  ...require('../src/main/services/runtimeSettings.ts'),
}));
const harness = path.resolve(__dirname, '../..');
const docker = process.env.SKILLZ_TEST_MCP_DOCKER === '1';

test('desktop pipes enforce workspace capabilities and deliver the artifact preview', { timeout: docker ? 420_000 : 30_000 }, async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'skillz-mcp-'));
  const prior = Object.fromEntries(['SKILLZ_MCP_HOME', 'PYTHON_AGENT_PYTHON', 'PYTHONPATH'].map(key => [key, process.env[key]]));
  process.env.SKILLZ_MCP_HOME = path.join(root, 'private');
  const repoPython = path.join(harness, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  if (fs.existsSync(repoPython)) process.env.PYTHON_AGENT_PYTHON = repoPython;
  process.env.PYTHONPATH = [harness, prior.PYTHONPATH].filter(Boolean).join(path.delimiter);
  const host = new McpHost(path.join(root, 'mcp-python'), () => harness);
  const fixture = path.join(root, 'agent'), repository = path.join(root, 'repo');
  const artifact = path.join(root, 'first-artifact'), otherArtifact = path.join(root, 'second-artifact');
  for (const directory of [fixture, repository, artifact, otherArtifact]) fs.mkdirSync(directory);
  if (docker) for (const directory of [artifact, otherArtifact]) execFileSync('git', ['init', '-q', directory]);
  const services = [];
  const sandboxRoots = [artifact, otherArtifact];
  let workbench;
  t.after(async () => {
    await workbench?.dispose();
    await Promise.all(services.map(async (agent) => {
      const exited = agent.process ? once(agent.process, 'close') : Promise.resolve();
      await agent.stop();
      await exited;
    }));
    if (docker) await removeArtifactDependencyVolumes(sandboxRoots, root);
    for (const [key, value] of Object.entries(prior)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    fs.rmSync(root, { recursive: true, force: true });
  });
  if (!await host.ready()) { t.skip('Install requirements-mcp.txt in the Skillz interpreter.'); return; }

  const server = path.join(root, 'fixture.cjs');
  fs.writeFileSync(server, `
const label = process.argv[2];
require('node:readline').createInterface({ input: process.stdin }).on('line', line => {
  const request = JSON.parse(line);
  if (!('id' in request)) return;
  const result = request.method === 'initialize'
    ? { protocolVersion: request.params.protocolVersion, capabilities: { tools: {} }, serverInfo: { name: label, version: '1' } }
    : request.method === 'tools/list'
      ? { tools: [{ name: 'provider_find', inputSchema: { type: 'object' } }] }
      : { content: [{ type: 'text', text: label + ' result' }] };
  console.log(JSON.stringify({ jsonrpc: '2.0', id: request.id, result }));
});`);
  const config = (label) => ({ transport: 'stdio', command: process.execPath, args: [server, label], timeout: 5 });
  await host.save({ root: repository, profile: 'repo' }, 'repo-hub', config('repo'));
  await host.save({ root: artifact, profile: 'artifact:first' }, 'artifact-hub', config('artifact'));
  assert.equal((await host.profile({ root: otherArtifact, profile: 'artifact:second' })).servers.playwright.enabled, false);
  const catalog = await host.inspect({ root: artifact, profile: 'artifact:first' }, 'artifact-hub', 'tools', 'provider');
  assert.equal(catalog.items[0].name, 'provider_find');

  for (const file of ['artifact_agent_entry.py', 'artifact_preview.py', 'mcp_runtime.py', 'mcp_settings.py', 'mcp_oauth.py']) {
    fs.copyFileSync(path.join(harness, file), path.join(fixture, file));
  }
  fs.writeFileSync(path.join(fixture, 'agent_tools.py'), '');
  fs.writeFileSync(path.join(fixture, 'artifact_model_host.py'), 'import json, sys\njson.load(sys.stdin)\nprint(\'{"ready":true}\')\n');
  fs.writeFileSync(path.join(fixture, 'main.py'), `
import json, os, pathlib, sys
from artifact_preview import run_artifact_preview, start_artifact_preview
from mcp_runtime import invoke_mcp
class BaseModelClient: pass
def main():
    root = pathlib.Path(sys.argv[sys.argv.index('--root') + 1])
    for line in sys.stdin:
        message = json.loads(line)
        prompt = message.get('text', '')
        action = json.loads(prompt.rsplit('\\n\\nUser request:\\n', 1)[-1]) if prompt else {}
        result = {}
        if action.get('operation') == 'start_preview':
            result = start_artifact_preview()
        elif action.get('operation') == 'verify_app':
            result = run_artifact_preview(action['action'])
        elif action.get('operation') == 'report_prompt':
            result = prompt
        elif action:
            result = invoke_mcp(root, action)
        print(json.dumps({'id': message['id'], 'ok': True, 'mcpResult': result,
                         'privateHomeVisible': 'SKILLZ_MCP_HOME' in os.environ}), flush=True)
    return 0
`);

  const context = path.join(root, 'context');
  fs.mkdirSync(context);
  function sandbox(directory) {
    if (docker) return new ArtifactSandbox(directory, [], context);
    return {
      prepare: async () => ({ docker: 'docker', args: [] }),
      spawn: (_binary, _args, command) => {
        assert.equal(command[2], '/opt/skillz/artifact_agent_entry.py');
        assert.equal(command[command.indexOf('--root') + 1], '/repo');
        return spawn(process.env.PYTHON_AGENT_PYTHON || 'python3', [
          '-u', path.join(fixture, 'artifact_agent_entry.py'), ...command.slice(3),
        ], { cwd: fixture, env: process.env });
      },
      stop: async () => {},
    };
  }
  async function start(directory, profile, sandboxed) {
    const execution = sandboxed ? new ArtifactAgentExecution(async () => sandbox(directory),
      message => { if (docker) process.stderr.write(message); }) : undefined;
    const agent = new AgentService({ requireRoot: () => directory, current: () => ({ root: directory }) },
      () => {}, undefined, execution, host, profile);
    agent.agentRoot = () => fixture;
    services.push(agent);
    await agent.start({ provider: 'fixture', model: 'fixture' });
    return agent;
  }

  const standalone = await start(repository, 'repo', false);
  const first = await start(artifact, 'artifact:first', true);
  const second = await start(otherArtifact, 'artifact:second', true);
  async function ask(agent, action) { return (await agent.submit(JSON.stringify(action))).mcpResult; }
  assert.deepEqual((await ask(standalone, { operation: 'servers' })).servers.map(item => item.name), ['playwright', 'repo-hub']);
  assert.deepEqual((await ask(first, { operation: 'servers' })).servers.map(item => item.name), ['artifact-hub']);
  assert.deepEqual((await ask(second, { operation: 'servers' })).servers, []);
  const call = { operation: 'call', server: 'artifact-hub', name: 'provider_find' };
  const result = await ask(first, call);
  assert.equal(result.result.content[0].text, 'artifact result');
  if (docker) assert.equal((await first.submit(JSON.stringify(call))).privateHomeVisible, false);
  assert.equal((await ask(second, call)).ok, false);
  await host.manage({ root: artifact, profile: 'artifact:first' }, 'disable', 'artifact-hub');
  assert.equal((await ask(first, call)).ok, false);

  const installer = new ArtifactLibraryService(
    path.join(root, 'artifact-settings.json'), path.join(harness, 'desktop/artifact-template'),
    path.join(root, 'contexts'), path.join(harness, 'desktop/prebuilt-artifacts'),
    (id, directory) => host.enablePlaywright({ root: directory, profile: `artifact:${id}` }),
  );
  await installer.configure(path.join(root, 'library'));
  const options = { title: 'Fixture', prompt: 'Fixture', sourceRoot: '', shareFacts: false, shareMemory: false };
  const plain = await installer.create(options);
  const optedIn = await installer.create({ ...options, playwrightMcp: true });
  const prebuilt = await installer.installPrebuilt('beta-observatory', { directories: [], allowWorkspaceRead: false }, undefined, true);
  for (const [record, enabled] of [[plain, false], [optedIn, true], [prebuilt, true]]) {
    const profile = await host.profile({ root: record.root, profile: `artifact:${record.id}` });
    assert.equal(profile.servers.playwright.enabled, enabled);
    assert.doesNotMatch(fs.readFileSync(path.join(record.root, 'artifact.json'), 'utf8'), /playwrightMcp/);
  }

  workbench = new ArtifactsService(installer, new RuntimeSettingsService(path.join(root, 'runtime.json')),
    event => { if (docker && event.type === 'runtime' && event.runtime.error) console.error(event.runtime.error); }, () => '', host);
  t.mock.method(AgentService.prototype, 'agentRoot', () => fixture);
  t.mock.method(workbench.capabilities, 'hostContext', async context => context);
  if (docker) {
    fs.writeFileSync(path.join(optedIn.root, 'package.json'), JSON.stringify({
      name: 'skillz-preview-fixture', private: true, type: 'module', dependencies: { tsx: '4.20.6' },
    }));
    fs.writeFileSync(path.join(optedIn.root, 'server/index.ts'), `
import { createServer } from 'node:http';
const port = Number(process.env.SKILLZ_ARTIFACT_PORT);
createServer((_, response) => {
  response.setHeader('content-type', 'text/html');
  response.end('<title>Artifact preview fixture</title><h1>Served by Docker</h1>');
}).listen(port, process.env.SKILLZ_ARTIFACT_HOST, () => {
  console.log('SKILLZ_ARTIFACT_READY ' + JSON.stringify({ url: 'http://127.0.0.1:' + port }));
});`);
    sandboxRoots.push(plain.root, optedIn.root);
  } else {
    t.mock.method(workbench, 'sandbox', async id => sandbox((await installer.find(id)).root));
    t.mock.method(workbench, 'start', async id => {
      assert.equal(id, optedIn.id);
      return { status: 'running', url: 'http://127.0.0.1:49153' };
    });
    t.mock.method(workbench, 'runPreviewAction', async (id, action) => ({
      ok: true, id, action,
    }));
  }
  const selection = { provider: 'gemini', model: 'fixture' };
  const launch = { operation: 'start_preview' };
  const reportPrompt = JSON.stringify({ operation: 'report_prompt' });
  await workbench.startAgent(plain.id, selection);
  assert.doesNotMatch((await workbench.submit(plain.id, reportPrompt)).mcpResult, /Playwright MCP|Start Preview/);
  await workbench.startAgent(optedIn.id, selection);
  const originalRequest = (await workbench.submit(optedIn.id, reportPrompt)).mcpResult;
  assert.match(originalRequest, /Playwright MCP is enabled for this artifact\. Use Start Preview/);
  assert.ok(originalRequest.endsWith(reportPrompt));
  assert.equal((await ask(standalone, launch)).ok, false);

  const previewAgent = await workbench.agent(optedIn.id);
  const preview = await ask(previewAgent, launch);
  assert.equal(preview.ok, true, JSON.stringify(preview));
  assert.match(preview.url, /^http:\/\/127\.0\.0\.1:\d+$/);
  assert.equal(preview.url, (await ask(previewAgent, launch)).url);
  const appAction = { operation: 'mcp', input: { operation: 'call', server: 'game-tools', name: 'read_board' } };
  const verifyApp = { operation: 'verify_app', action: appAction };
  assert.equal((await ask(standalone, verifyApp)).ok, false);
  if (!docker) {
    assert.deepEqual(await ask(previewAgent, verifyApp), { ok: true, id: optedIn.id, action: appAction });
  }
  if (docker) {
    assert.equal(preview.url, (await workbench.start(optedIn.id)).url);
    assert.match(await (await fetch(preview.url)).text(), /Served by Docker/);
    const browser = await ask(previewAgent, { operation: 'call', server: 'playwright', name: 'browser_navigate',
      arguments: { url: preview.url } });
    assert.equal(browser.ok, true, JSON.stringify(browser));
    assert.match(JSON.stringify(browser.result), /Page Title: Artifact preview fixture/);
  }
  await host.manage({ root: optedIn.root, profile: `artifact:${optedIn.id}` }, 'disable', 'playwright');
  assert.doesNotMatch((await workbench.submit(optedIn.id, reportPrompt)).mcpResult, /Playwright MCP|Start Preview/);
  assert.equal(preview.url, (await ask(previewAgent, launch)).url);
});
