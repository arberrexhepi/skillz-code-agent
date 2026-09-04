const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const load = require('./load-ts.cjs');
const { ArtifactProcessProxy } = load(() => require('../src/main/services/artifactProcessProxy.ts'));

function fixture(t, allowed = true, allowlist) {
  const directory = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'skillz process proxy ')));
  fs.writeFileSync(path.join(directory, 'package.json'), JSON.stringify({
    scripts: {
      dev: `node -e "console.log('\\u001b[36mhttp://localhost:\\u001b[1m43127\\u001b[0m')"`,
      test: `node -e "console.log('must not run')"`,
    },
  }));
  const proxy = new ArtifactProcessProxy([{ id: 'project', label: 'Project', path: directory, access: 'read', allowProcessProxy: allowed, processProxyAllowlist: allowlist }]);
  t.after(async () => { await proxy.close(); fs.rmSync(directory, { recursive: true, force: true }); });
  return { directory, proxy };
}

async function invoke(connection, body, token = connection.token) {
  const target = new URL(connection.url.replace('host.docker.internal', '127.0.0.1') + '/v1/npm');
  const data = Buffer.from(JSON.stringify(body));
  return new Promise((resolve, reject) => {
    const request = http.request(target, { method: 'POST', headers: { 'content-type': 'application/json', 'content-length': String(data.length), 'x-skillz-process-token': token } }, response => {
      let output = '';
      response.setEncoding('utf8'); response.on('data', chunk => output += chunk); response.on('end', () => resolve({ status: response.statusCode, output }));
    });
    request.on('error', reject); request.end(data);
  });
}

test('Process Proxy runs only approved startup scripts from explicitly enabled grants and streams output', async t => {
  const { proxy } = fixture(t);
  const connection = await proxy.start();
  const result = await invoke(connection, { cwd: '/reads/project', args: ['run', 'dev'] });
  assert.equal(result.status, 200);
  const messages = result.output.trim().split('\n').map(JSON.parse);
  const stdout = Buffer.concat(messages.filter(message => message.stream === 'stdout').map(message => Buffer.from(message.data, 'base64'))).toString('utf8');
  assert.match(stdout, /http:\/\/localhost:/);
  assert.match(stdout, /Process Proxy listening: http:\/\/127\.0\.0\.1:43127/);
  assert.equal(messages.at(-1).exitCode, 0);
  assert.deepEqual(proxy.origins(), [], 'completed processes no longer expose an origin');
  const forbidden = await invoke(connection, { cwd: '/reads/project', args: ['run', 'test'] });
  assert.equal(forbidden.status, 400); assert.match(forbidden.output, /test is disallowed by the Process Proxy allowlist/);
  const unauthorized = await invoke(connection, { cwd: '/reads/project', args: ['run', 'dev'] }, 'wrong');
  assert.equal(unauthorized.status, 401);
});

test('Process Proxy runs explicitly whitelisted utility scripts and disallows startup defaults once customized', async t => {
  const { proxy } = fixture(t, true, ['test']);
  const connection = await proxy.start();
  const utility = await invoke(connection, { cwd: '/reads/project', args: ['run', 'test'] });
  assert.equal(utility.status, 200);
  const launch = await invoke(connection, { cwd: '/reads/project', args: ['run', 'dev'] });
  assert.equal(launch.status, 400); assert.match(launch.output, /dev is disallowed/);
});

test('Process Proxy rejects repositories without a separate execution grant', async t => {
  const { proxy } = fixture(t, false);
  const connection = await proxy.start();
  const result = await invoke(connection, { cwd: '/reads/project', args: ['run', 'dev'] });
  assert.equal(result.status, 400);
  assert.match(result.output, /not allowed.*Enable it in File access/);
});
