import { spawn, type ChildProcess } from 'node:child_process';
import type { Express } from 'express';

const active = new Set<ChildProcess>();
export function attachArtifactAgent(app: Express): () => void {
  app.post('/_skillz/agent/chat', (request, response) => {
    if (!process.env.SKILLZ_MODEL_BROKER_URL) { response.status(404).json({ error: 'Skillz chatbot is not enabled. Enable it under App capabilities and restart the preview.' }); return; }
    if (active.size >= 4) { response.status(429).json({ error: 'Too many chatbot requests are running.' }); return; }
    const payload = request.body;
    if (!payload || typeof payload !== 'object' || typeof payload.message !== 'string' || !payload.message.trim() || payload.message.length > 100_000) { response.status(400).json({ error: 'A message smaller than 100 KB is required.' }); return; }
    const child = spawn('python3', ['-u', '/opt/skillz/artifact_app_agent.py'], { cwd: '/repo', env: process.env, shell: false, stdio: ['pipe', 'pipe', 'pipe'] }); active.add(child);
    let output = '', errors = ''; const timeout = setTimeout(() => child.kill('SIGKILL'), 1_200_000);
    child.stdout?.setEncoding('utf8'); child.stderr?.setEncoding('utf8'); child.stdout?.on('data', text => { output = (output + text).slice(-2_000_000); }); child.stderr?.on('data', text => { errors = (errors + text).slice(-10_000); });
    child.on('error', error => { clearTimeout(timeout); active.delete(child); if (!response.headersSent) response.status(500).json({ error: error.message }); });
    child.on('close', code => { clearTimeout(timeout); active.delete(child); if (response.headersSent) return; try { const value = JSON.parse(output.trim()); if (code !== 0 || value.error) response.status(502).json({ error: value.error || errors || `Chatbot exited with code ${code}.` }); else response.json(value); } catch { response.status(502).json({ error: errors || 'Chatbot returned invalid JSON.' }); } });
    child.stdin?.end(JSON.stringify({ message: payload.message, history: Array.isArray(payload.history) ? payload.history : [], approveMutations: payload.approveMutations === true }));
  });
  return () => { for (const child of active) child.kill('SIGKILL'); active.clear(); };
}
