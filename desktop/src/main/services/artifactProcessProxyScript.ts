export const artifactProcessProxyScript = String.raw`#!/usr/bin/env node
const http = require('node:http');
const { spawn } = require('node:child_process');

const args = process.argv.slice(2);
const cwd = process.cwd();
const endpoint = process.env.SKILLZ_PROCESS_PROXY_URL;
const token = process.env.SKILLZ_PROCESS_PROXY_TOKEN;

function localNpm() {
  const child = spawn('/usr/local/bin/npm', args, { cwd, env: process.env, stdio: 'inherit' });
  child.on('error', (error) => { console.error(error.message); process.exitCode = 1; });
  child.on('exit', (code, signal) => {
    if (signal) process.kill(process.pid, signal);
    else process.exitCode = code ?? 1;
  });
}

if (!endpoint || !token || !cwd.startsWith('/reads/')) {
  localNpm();
} else {
  const body = Buffer.from(JSON.stringify({ cwd, args }));
  const target = new URL('/v1/npm', endpoint);
  const request = http.request(target, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'content-length': String(body.length),
      'x-skillz-process-token': token,
    },
  });
  let buffer = '';
  let finalCode;
  request.on('response', (response) => {
    response.setEncoding('utf8');
    response.on('data', (chunk) => {
      buffer += chunk;
      let newline;
      while ((newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1);
        if (!line) continue;
        try {
          const message = JSON.parse(line);
          if (message.stream === 'stdout' || message.stream === 'stderr') {
            process[message.stream].write(Buffer.from(message.data, 'base64'));
          } else if (Number.isInteger(message.exitCode)) finalCode = message.exitCode;
          else if (message.error) process.stderr.write('Process Proxy: ' + message.error + '\n');
        } catch { process.stderr.write('Process Proxy returned an invalid response.\n'); }
      }
    });
    response.on('end', () => {
      if (response.statusCode !== 200) {
        if (buffer.trim()) process.stderr.write(buffer.trim() + '\n');
        process.exitCode = 1;
      } else process.exitCode = Number.isInteger(finalCode) ? finalCode : 1;
    });
  });
  request.on('error', (error) => { console.error('Process Proxy: ' + error.message); process.exitCode = 1; });
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => {
    request.destroy();
    process.exit(128 + (signal === 'SIGINT' ? 2 : 15));
  });
  request.end(body);
}
`;
