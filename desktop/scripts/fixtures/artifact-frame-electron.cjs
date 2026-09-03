// The product iframe and frame policy under test in Electron. No Playwright or Docker needed.
const { app, BrowserWindow } = require('electron');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { buildSync } = require('esbuild');
const { WebSocketServer } = require('ws');
const load = require('../load-ts.cjs');
const { installArtifactFrameSecurity } = load(() => require('../../src/main/services/artifactFrameSecurity.ts'));
const { registerIpc } = load(() => require('../../src/main/ipc.ts'));
const root = process.argv[2]; app.setPath('userData', path.join(root, 'electron'));
const servers = [], sockets = new Set(), messages = [];
let window, dispose;
async function serve(handler) {
  const server = http.createServer(handler); servers.push(server);
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  return { server, url: `http://127.0.0.1:${server.address().port}` };
}
async function until(fn, message) {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const result = await fn(); if (result) return result;
    await new Promise(resolve => setTimeout(resolve, 40));
  }
  throw new Error(message);
}
async function run() {
  let externalRequests = 0;
  const external = await serve((_req, res) => { externalRequests++; res.setHeader('Access-Control-Allow-Origin', '*'); res.end('external'); });
  const artifact = await serve((req, res) => {
    if (req.url === '/redirect') { res.writeHead(302, { Location: external.url }); res.end(); return; }
    if (req.url === '/api/data' || req.url.startsWith('/files/')) {
      const allowed = !req.headers.origin || req.headers.origin === artifact.url;
      res.writeHead(allowed ? 200 : 403, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ value: 'Readable ë', origin: req.headers.origin || null })); return;
    }
    if (req.url === '/worker.js') { res.setHeader('Content-Type', 'text/javascript'); res.end("self.onmessage=()=>postMessage('worker ready')"); return; }
    if (req.url === '/module.js') {
      res.setHeader('Content-Type', 'text/javascript');
      res.end(`window.moduleReady = true; document.querySelector('#increment').onclick = () => document.querySelector('#count').textContent = Number(document.querySelector('#count').textContent)+1;`); return;
    }
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.setHeader('Content-Security-Policy', "default-src * 'unsafe-inline' 'unsafe-eval' blob: data:");
    res.end(`<!doctype html><meta charset="utf-8"><style>body{font:18px system-ui;margin:24px;background:#12251e;color:#e0fff1}input{width:75%;padding:12px}button{padding:12px}</style>
      <h1>Live artifact</h1><label>Filter imports <input id="filter"></label><p><button id="increment">Expand graph</button> <output id="count">0</output></p><script type="module" src="/module.js"></script>`);
  });
  const websocket = new WebSocketServer({ server: artifact.server });
  websocket.on('connection', client => client.on('message', data => client.send(data, { binary: false })));
  const live = new Set([artifact.url, external.url]);
  const desktop = path.resolve(__dirname, '../..');
  buildSync({ entryPoints: [path.join(desktop, 'src/preload/index.ts')], bundle: true, platform: 'node', format: 'cjs', external: ['electron'], outfile: path.join(root, 'preload.cjs') });
  const bundle = buildSync({ stdin: { contents: `
    import React, {useState} from 'react'; import {createRoot} from 'react-dom/client';
    import {ArtifactPreview} from './src/renderer/src/components/ArtifactPreview';
    function Test(){const [active,setActive]=useState(true);const [running,setRunning]=useState(true);
      window.togglePreview=()=>setActive(value=>!value);window.setRunning=setRunning;
      return <main style={{height:'100vh',display:'flex',flexDirection:'column'}}><ArtifactPreview title="Imports" active={active} runtime={{id:'one',status:running?'running':'stopped',url:${JSON.stringify(artifact.url)},logs:''}} />{!active&&<p>File access panel</p>}</main>;}
    createRoot(document.getElementById('root')).render(<Test/>);
  `, resolveDir: desktop, loader: 'tsx' }, bundle: true, format: 'iife', jsx: 'automatic', write: false }).outputFiles[0].text;
  const css = fs.readFileSync(path.join(desktop, 'src/renderer/src/artifacts.css'), 'utf8');
  const workbench = await serve((req, res) => {
    if (req.url === '/bundle.js') { res.setHeader('Content-Type', 'text/javascript'); res.end(bundle); return; }
    res.setHeader('Content-Type', 'text/html');
    res.end(`<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-src http://127.0.0.1:*"><style>html,body{margin:0}${css}</style><div id="root"></div><script src="/bundle.js"></script>`);
  });
  window = new BrowserWindow({ show: false, width: 1200, height: 900, webPreferences: { preload: path.join(root, 'preload.cjs'), sandbox: true, contextIsolation: true, nodeIntegration: false, nodeIntegrationInSubFrames: false } });
  const wc = window.webContents;
  wc.on('console-message', details => messages.push(details.message));
  dispose = installArtifactFrameSecurity(wc, () => [...live]);
  wc.setWindowOpenHandler(() => ({ action: 'deny' }));
  registerIpc(window, { agent: { setCodexCliPath: () => ({ saved: true }) } });
  await window.loadURL(workbench.url);
  const frame = await until(async () => {
    for (const frame of wc.mainFrame.frames) if (frame.url.startsWith(artifact.url) && await frame.executeJavaScript('window.moduleReady === true').catch(() => false)) return frame;
  }, 'Artifact module did not load');
  const js = code => frame.executeJavaScript(code);
  assert.equal(await wc.executeJavaScript('typeof window.workbench'), 'object');
  for (const name of ['window.workbench', 'require', 'process']) assert.equal(await js(`typeof ${name}`), 'undefined');
  assert.equal(await js(`(()=>{try{return typeof parent.workbench}catch{return 'blocked'}})()`), 'blocked');
  assert.deepEqual(await wc.executeJavaScript(`window.workbench.agent.setCodexCliPath(null)`), { saved: true });
  const initial = await js('({width:innerWidth,height:innerHeight,dpr:devicePixelRatio})');
  assert.ok(initial.width > 1100 && initial.height > 600, JSON.stringify(initial));
  await js(`document.querySelector('#filter').focus()`);
  await wc.insertText('imports ë 🧩');
  assert.equal(await js(`document.querySelector('#filter').value`), 'imports ë 🧩');
  await js(`document.querySelector('#increment').click()`);
  assert.equal(await js(`document.querySelector('#count').textContent`), '1');
  await wc.executeJavaScript('window.togglePreview()');
  await until(() => wc.executeJavaScript(`document.querySelector('.artifact-preview').hidden`), 'Preview did not hide');
  await wc.executeJavaScript('window.togglePreview()');
  await until(() => wc.executeJavaScript(`!document.querySelector('.artifact-preview').hidden`), 'Preview did not return');
  assert.equal(await js(`document.querySelector('#filter').value`), 'imports ë 🧩');
  window.setSize(1500, 1000);
  await until(async () => (await js('innerWidth')) > initial.width + 200, 'Iframe did not resize with window');
  const data = await js(`fetch('/files/documents/read',{method:'POST'}).then(r=>r.json())`);
  assert.equal(data.value, 'Readable ë'); assert.equal(data.origin, artifact.url);
  assert.equal(await js(`new Promise((resolve,reject)=>{const s=new WebSocket(${JSON.stringify(artifact.url.replace('http:', 'ws:'))});s.onopen=()=>s.send('live ë');s.onmessage=e=>{resolve(e.data);s.close()};s.onerror=reject})`), 'live ë');
  assert.equal(await js(`fetch(${JSON.stringify(external.url)}).then(()=>false,()=>true)`), true);
  assert.equal(await js(`new Promise(resolve=>{const s=document.createElement('script');s.src=${JSON.stringify(external.url + '/script.js')};s.onerror=()=>resolve(true);s.onload=()=>resolve(false);document.head.append(s)})`), true);
  assert.equal(await js(`new Promise((resolve,reject)=>{const w=new Worker('/worker.js');w.onmessage=e=>{resolve(e.data);w.terminate()};w.onerror=reject;w.postMessage('go')})`), 'worker ready');
  assert.equal(await js(`new Promise(resolve=>{const img=new Image();img.onload=()=>resolve(true);img.onerror=()=>resolve(false);img.src='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>')})`), true);
  assert.equal(await js(`navigator.serviceWorker.register('/module.js').then(()=>false,()=>true)`), true);
  assert.equal(await js(`Notification.requestPermission()`), 'denied');
  assert.equal(await js(`window.open(${JSON.stringify(external.url)})===null`), true);
  await js(`location.href=${JSON.stringify(workbench.url)};void 0`);
  await new Promise(resolve => setTimeout(resolve, 150));
  assert.ok(frame.url.startsWith(artifact.url));
  await js(`location.href='/redirect';void 0`).catch(() => {});
  await new Promise(resolve => setTimeout(resolve, 200));
  assert.equal(externalRequests, 0);
  assert.equal(wc.getURL(), workbench.url + '/');
  await wc.executeJavaScript(`document.querySelector('.artifact-preview-tools button').click()`);
  const reloaded = await until(async () => {
    for (const next of wc.mainFrame.frames) if (next !== frame && next.url.startsWith(artifact.url) && await next.executeJavaScript('window.moduleReady === true').catch(() => false)) return next;
  }, 'Preview did not reload');
  assert.equal(await reloaded.executeJavaScript(`document.querySelector('#filter').value`), '');
  live.delete(artifact.url);
  assert.equal(await reloaded.executeJavaScript(`fetch('/api/data').then(()=>false,()=>true)`), true);
  await wc.executeJavaScript('window.setRunning(false)');
  await until(() => wc.executeJavaScript(`document.querySelector('iframe')===null`), 'Stopped preview remained mounted');
  console.log('Live iframe checks passed: native input, resize, state, reload, HTTP/WebSocket, CSP, navigation, permissions, IPC boundary and revocation.');
}
app.whenReady().then(run).then(() => finish(0), error => { console.error(error); console.error(messages.join('\n')); return finish(1); });
async function finish(code) {
  if (dispose) dispose();
  if (window && !window.isDestroyed()) window.destroy();
  for (const socket of sockets) socket.destroy();
  await Promise.all(servers.map(server => new Promise(resolve => server.close(resolve))));
  app.exit(code);
}
