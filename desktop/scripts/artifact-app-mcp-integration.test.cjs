const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const load = require('./load-ts.cjs');

const { ArtifactLibraryService, ArtifactsService, RuntimeSettingsService, dockerCommand, removeArtifactDependencyVolumes } = load(() => ({
  ...require('../src/main/services/artifactLibrary.ts'), ...require('../src/main/services/artifacts.ts'),
  ...require('../src/main/services/runtimeSettings.ts'), ...require('../src/main/services/artifactSandbox.ts'),
  ...require('../src/main/services/artifactDockerCleanup.ts'),
}));
const harness = path.resolve(__dirname, '../..');
const exec = promisify(execFile);

const fixture = `
import os
import sys
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pathlib import Path

origin = sys.argv[1]
server = FastMCP("fixture", port=39169, stateless_http=True, json_response=True)

@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def identity() -> dict[str, str | bool | None]:
    return {"origin": origin, "dockerenv": Path("/.dockerenv").exists(),
            "profile": os.environ.get("SKILLZ_MCP_HOME"), "credential": bool(os.environ.get("ARTIFACT_MCP_FIXTURE_KEY"))}

@server.tool()
def record(value: str) -> str:
    Path(".mcp-fixture-write").write_text(value)
    return value

@server.resource("fixture://brief/main")
def brief() -> str:
    return "brief from " + origin

server.run(transport=sys.argv[2] if len(sys.argv) > 2 else "stdio")
`;

async function post(url, payload, headers = {}) {
  const response = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json', ...headers }, body: JSON.stringify(payload) });
  return { status: response.status, data: await response.json() };
}

test('artifact MCP installs in Docker and serves its own manual, protocol and chatbot clients', { timeout: 480_000 }, async t => {
  const root = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'skillz-app-mcp-')));
  const library = new ArtifactLibraryService(path.join(root, 'settings.json'), path.join(harness, 'desktop/artifact-template'), path.join(root, 'contexts'));
  const service = new ArtifactsService(library, new RuntimeSettingsService(path.join(root, 'runtime.json')), event => {
    if (event.type === 'runtime' && event.runtime.error) console.error(event.runtime.error, event.runtime.logs.slice(-2500));
  });
  const roots = [];
  const credential = process.env.ARTIFACT_MCP_FIXTURE_KEY;
  process.env.ARTIFACT_MCP_FIXTURE_KEY = 'fixture-local-value';
  t.after(async () => {
    await service.dispose();
    if (credential === undefined) delete process.env.ARTIFACT_MCP_FIXTURE_KEY;
    else process.env.ARTIFACT_MCP_FIXTURE_KEY = credential;
    await removeArtifactDependencyVolumes(roots, root);
    await fs.rm(root, { recursive: true, force: true });
  });
  await library.configure(path.join(root, 'library'));
  const defaults = { prompt: 'A project with an MCP-backed app', sourceRoot: '', shareFacts: false, shareMemory: false };
  const first = await library.create({ ...defaults, title: 'First MCP',
    runtime: { provider: 'gemini', model: 'fixture', backendScript: 'main_v2.py' } });
  const second = await library.create({ ...defaults, title: 'Second MCP' });
  roots.push(first.root, second.root);
  for (const artifact of [first, second]) await fs.writeFile(path.join(artifact.root, 'fixture.py'), fixture);

  let finishBroadcast;
  const broadcastObserved = new Promise(resolve => { finishBroadcast = resolve; });
  // Model replies are deterministic. The app chatbot, container subprocess,
  // scoped model broker, and MCP operations are the normal production paths.
  t.mock.method(service.capabilities, 'modelRequest', async (_selection, request) => {
    const messages = request.messages;
    if (messages[0].content === 'stream demo') {
      if (messages.length === 1) return { text: JSON.stringify({ type: 'broadcast', content: 'Checking the preview.' }) };
      if (messages.at(-1).content === 'Landscape') {
        assert.match(messages.at(-2).content, /Which layout/);
        return { text: JSON.stringify({ type: 'answer', content: 'Layout selected.' }) };
      }
      await broadcastObserved;
      return { text: JSON.stringify({ type: 'elicitation', question: 'Which layout?', choices: ['Portrait', 'Landscape'] }) };
    }
    assert.match(request.system, /Artifact MCP servers available to this chatbot/);
    assert.doesNotMatch(request.system, /private-tool/);
    if (messages[0].content === 'Verify artifact status') {
      assert.match(request.system, /authored-tools/);
      const action = messages.length === 1
        ? { type: 'mcp', operation: 'call', server: 'authored-tools', name: 'read_artifact_status' }
        : { type: 'answer', content: 'The app route is live and the approved note persisted.' };
      if (messages.length > 1) {
        assert.match(messages.at(-1).content, /approved starter note/);
        assert.match(messages.at(-1).content, /"route_ok": true/);
      }
      return { text: JSON.stringify(action) };
    }
    if (messages[0].content === 'Verify artifact write approval') {
      const action = messages.length === 1
        ? { type: 'mcp', operation: 'call', server: 'authored-tools', name: 'save_note', arguments: { text: 'unapproved chatbot note' } }
        : { type: 'answer', content: 'The write requires approval.' };
      if (messages.length > 1) assert.match(messages.at(-1).content, /requires explicit mutation approval/);
      return { text: JSON.stringify(action) };
    }
    let action;
    switch (messages.length) {
      case 1: action = { type: 'mcp', operation: 'call', server: 'private-tool', name: 'identity' }; break;
      case 3:
        assert.match(messages.at(-1).content, /unavailable for this caller/);
        action = { type: 'mcp', operation: 'call', server: 'scene', name: 'record', arguments: { value: 'chat approved' } }; break;
      case 5:
        if (messages[0].content === 'read') assert.match(messages.at(-1).content, /requires explicit mutation approval/);
        else assert.match(messages.at(-1).content, /chat approved/);
        action = { type: 'mcp', operation: 'read-resource', server: 'scene', uri: 'fixture://brief/main' }; break;
      default:
        assert.match(messages.at(-1).content, /brief from first/);
        action = { type: 'answer', content: 'Read the first artifact brief.' };
    }
    return { text: JSON.stringify(action) };
  });
  const executable = (id, origin, chatbot = false) => ({
    id, transport: 'stdio', command: 'python', args: ['/repo/fixture.py', origin], chatbot, env: ['ARTIFACT_MCP_FIXTURE_KEY'],
  });
  // Upgrade a working pre-MCP artifact, including a customized app route.
  const entry = path.join(first.root, 'server/index.ts');
  const preMcp = (await fs.readFile(entry, 'utf8')).replace(/^.*attachArtifactMcp.*\n/gm, '').replace('closeMcp(); ', '')
    .replace('const closeAgent =', "app.get('/preserved', (_request, response) => response.json({ ok: true }));\nconst closeAgent =");
  await fs.writeFile(entry, preMcp);
  await fs.unlink(path.join(first.root, 'server/mcp.ts'));
  await fs.unlink(path.join(first.root, 'src/mcp.ts'));
  const migration = await service.saveApp(first.id, { version: 1, chatbot: { enabled: true }, mcp: { enabled: true, servers: [
    executable('scene', 'first', true), executable('private-tool', 'private'),
    { id: 'container-http', transport: 'http', url: 'http://127.0.0.1:39169/mcp' },
    { id: 'authored-tools', transport: 'stdio', source: 'artifact', chatbot: true },
  ] } });
  assert.ok(migration.migrated && migration.commit && migration.checkpointCommit);
  assert.match(await fs.readFile(entry, 'utf8'), /preserved/);
  const authoredSource = path.join(first.root, 'mcp/authored-tools/server.py');
  const starter = await fs.readFile(authoredSource, 'utf8');
  assert.match(starter, /def get_note/);
  await assert.rejects(service.saveApp(second.id, {version: 1, mcp: {enabled: true, servers: [
    { id: 'host-server', transport: 'http', url: 'https://host.docker.internal/mcp' },
  ]}}), /Use an HTTPS MCP URL or HTTP loopback/);
  await service.saveApp(second.id, { version: 1, mcp: { enabled: true, servers: [executable('other-scene', 'second')] } });

  const [left, right] = await Promise.all([service.start(first.id), service.start(second.id)]);
  assert.match(left.url, /^http:\/\/127\.0\.0\.1:/);
  assert.notEqual(left.url, right.url);
  assert.equal((await fetch(left.url + '/preserved')).status, 200);
  const leftContainer = service.runtimes.get(first.id).sandbox.name;
  const docker = await dockerCommand();
  const bundledSkills = await exec(docker, ['exec', leftContainer, 'python3', '-c',
    "import sys; from pathlib import Path; sys.path.insert(0, '/opt/skillz'); from skill_loader import load_markdown_skills_from_dir; print(*(s.name for s in load_markdown_skills_from_dir(Path('/opt/skillz/skills'))))"]);
  assert.match(bundledSkills.stdout, /\bartifact-chatbot\b/);
  assert.match(bundledSkills.stdout, /\bartifact-mcp-authoring\b/);
  await exec(docker, ['exec', '--detach', leftContainer, '/opt/skillz-mcp/bin/python', '/repo/fixture.py', 'first-http', 'streamable-http']);
  await exec(docker, ['exec', leftContainer, '/opt/skillz-mcp/bin/python', '-c', 'import socket,time\nfor _ in range(50):\n try:\n  socket.create_connection(("127.0.0.1",39169),0.2).close(); break\n except OSError: time.sleep(0.1)\nelse: raise SystemExit(1)']);

  const manual = (action, id = first.id) => service.requestAppMcp(id, action);
  assert.deepEqual((await manual({ operation: 'servers' })).servers.map(server => server.name), ['scene', 'private-tool', 'container-http', 'authored-tools']);
  assert.deepEqual((await manual({ operation: 'servers' }, second.id)).servers.map(server => server.name), ['other-scene']);
  assert.equal((await manual({ operation: 'call', server: 'scene', name: 'identity' }, second.id)).ok, false);
  const identity = await manual({ operation: 'call', server: 'scene', name: 'identity' });
  assert.equal(identity.ok, true, JSON.stringify(identity));
  assert.deepEqual(identity.result.structuredContent, { origin: 'first', dockerenv: true, profile: null, credential: true });
  const httpIdentity = await manual({ operation: 'call', server: 'container-http', name: 'identity' });
  assert.equal(httpIdentity.ok, true, JSON.stringify(httpIdentity));
  assert.equal(httpIdentity.result.structuredContent.origin, 'first-http');
  const resources = await manual({ operation: 'list', server: 'scene', kind: 'resources' });
  assert.equal(resources.items[0].uri, 'fixture://brief/main');
  assert.match(JSON.stringify(await manual({ operation: 'read-resource', server: 'scene', uri: resources.items[0].uri })), /brief from first/);

  const mutation = { operation: 'call', server: 'scene', name: 'record', arguments: { value: 'manual approved' } };
  assert.match((await manual(mutation)).error, /requires explicit mutation approval/);
  await assert.rejects(fs.access(path.join(first.root, '.mcp-fixture-write')));
  assert.equal((await manual({ ...mutation, approveMutations: true })).ok, true);
  assert.equal(await fs.readFile(path.join(first.root, '.mcp-fixture-write'), 'utf8'), 'manual approved');

  const noteRead = { operation: 'call', server: 'authored-tools', name: 'get_note' };
  const noteWrite = { operation: 'call', server: 'authored-tools', name: 'save_note', arguments: { text: 'approved starter note' } };
  const tools = await manual({ operation: 'list', server: 'authored-tools', kind: 'tools' });
  assert.deepEqual(tools.items.map(tool => tool.name), ['get_note', 'save_note']);
  const noteDescription = name => manual({ operation: 'describe', server: 'authored-tools', kind: 'tools', name });
  assert.equal((await noteDescription('get_note')).item.annotations.readOnlyHint, true);
  assert.equal((await noteDescription('save_note')).item.annotations.readOnlyHint, false);
  assert.deepEqual((await manual(noteRead)).result.structuredContent, { note: null });
  assert.match((await service.runPreviewAction(first.id, { operation: 'mcp', input: noteWrite })).error, /requires explicit mutation approval/);
  await assert.rejects(service.runPreviewAction(first.id, { operation: 'mcp', input: { ...noteWrite, approveMutations: true } }));
  assert.deepEqual((await manual(noteRead)).result.structuredContent, { note: null });
  assert.deepEqual((await manual({ ...noteWrite, approveMutations: true })).result.structuredContent, { note: 'approved starter note' });

  const denied = await post(left.url + '/_skillz/mcp/request', { operation: 'servers' }, { origin: 'http://untrusted.example' });
  assert.equal(denied.status, 403);
  const protocol = { accept: 'application/json, text/event-stream' };
  async function rpc(id, method, params) {
    const response = await post(left.url + '/_skillz/mcp', { jsonrpc: '2.0', id, method, params }, protocol);
    assert.equal(response.status, 200, JSON.stringify(response.data));
    assert.ok(response.data.result, JSON.stringify(response.data));
    return response.data.result;
  }
  assert.equal((await rpc(1, 'initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'fixture', version: '1' } })).serverInfo.name, 'Skillz Artifact MCP');
  assert.ok((await rpc(2, 'tools/list', {})).tools.some(tool => tool.name === 'mcp_request'));
  const bridge = await rpc(3, 'tools/call', { name: 'mcp_request', arguments: { action: { operation: 'servers' } } });
  assert.ok(bridge.structuredContent.servers.some(server => server.name === 'scene'));
  const resource = await rpc(4, 'resources/read', { uri: 'artifact-mcp://resource/scene/fixture%3A%2F%2Fbrief%2Fmain' });
  assert.match(resource.contents[0].text, /brief from first/);

  const chatUrl = left.url + '/_skillz/agent/chat';
  assert.equal((await post(chatUrl, { message: 'read' })).data.message, 'Read the first artifact brief.');
  assert.equal(await fs.readFile(path.join(first.root, '.mcp-fixture-write'), 'utf8'), 'manual approved');
  assert.equal((await post(chatUrl, { message: 'write', approveMutations: true })).data.message, 'Read the first artifact brief.');
  assert.equal(await fs.readFile(path.join(first.root, '.mcp-fixture-write'), 'utf8'), 'chat approved');
  const streaming = await fetch(chatUrl, {
    method: 'POST', headers: { 'content-type': 'application/json', accept: 'application/x-ndjson' },
    body: JSON.stringify({ message: 'stream demo' }), signal: AbortSignal.timeout(15_000),
  });
  assert.match(streaming.headers.get('content-type'), /application\/x-ndjson/);
  const reader = streaming.body.getReader();
  const decoder = new TextDecoder();
  let frames = '';
  try {
    while (!frames.includes('\n')) {
      const chunk = await reader.read();
      assert.equal(chunk.done, false, 'The broadcast must arrive before the model can answer.');
      frames += decoder.decode(chunk.value, { stream: true });
    }
    assert.deepEqual(JSON.parse(frames.split('\n')[0]), { type: 'broadcast', content: 'Checking the preview.' });
    finishBroadcast();
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      frames += decoder.decode(chunk.value, { stream: true });
    }
  } finally { finishBroadcast(); await reader.cancel(); }
  const result = JSON.parse(frames.trim().split('\n')[1]).result;
  assert.deepEqual(result.elicitation, { question: 'Which layout?', choices: ['Portrait', 'Landscape'] });
  const answer = await post(chatUrl, {
    message: 'Landscape', history: [{ role: 'user', content: 'stream demo' }, { role: 'assistant', content: result.elicitation.question }],
  });
  assert.equal(answer.data.message, 'Layout selected.');
  assert.equal((await post(right.url + '/_skillz/agent/chat', { message: 'read' })).status, 404);
  assert.match((await exec(docker, ['exec', leftContainer, 'npm', 'run', 'build'])).stdout, /built in/);

  const main = 'if __name__ == "__main__":';
  assert.ok(starter.includes(main));
  await fs.writeFile(authoredSource, starter.replace(main, `
@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def read_artifact_status() -> dict[str, str | bool | None]:
    """Report the live app route and the note after restart."""
    import json
    from urllib.request import urlopen
    with urlopen(os.environ["SKILLZ_ARTIFACT_TOOL_ORIGIN"] + "/preserved", timeout=2) as response:
        return {"route_ok": json.load(response)["ok"], **get_note()}


${main}`));
  const restarted = await service.runPreviewAction(first.id, { operation: 'restart' });
  assert.equal(restarted.ok, true, JSON.stringify(restarted));
  const status = { operation: 'call', server: 'authored-tools', name: 'read_artifact_status' };
  const expected = { route_ok: true, note: 'approved starter note' };
  assert.deepEqual((await manual(status)).result.structuredContent, expected);
  assert.deepEqual((await service.runPreviewAction(first.id, { operation: 'mcp', input: status })).result.structuredContent, expected);
  const chat = await service.runPreviewAction(first.id, { operation: 'chat', input: { message: 'Verify artifact status' } });
  assert.equal(chat.ok, true, JSON.stringify(chat));
  assert.equal(chat.message, 'The app route is live and the approved note persisted.');
  assert.deepEqual(chat.mcpCalls, [{ server: 'authored-tools', name: 'read_artifact_status', ok: true }]);
  const unapprovedChat = await service.runPreviewAction(first.id, { operation: 'chat', input: { message: 'Verify artifact write approval' } });
  assert.deepEqual(unapprovedChat.mcpCalls, [{ server: 'authored-tools', name: 'save_note', ok: false }]);
  assert.deepEqual((await manual(noteRead)).result.structuredContent, { note: 'approved starter note' });
  assert.equal((await manual(status, second.id)).ok, false);
  await service.stop(second.id);
  await service.saveApp(second.id, { version: 1 });
  const disabled = await service.start(second.id);
  assert.equal((await post(disabled.url + '/_skillz/mcp/request', { operation: 'servers' })).status, 404);
});
