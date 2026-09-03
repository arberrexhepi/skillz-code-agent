export interface SharedFolder { id: string; label: string; readOnly: boolean; access: 'read' | 'write' }
async function checked(url: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(url, init);
  if (!response.ok) { const detail = await response.json().catch(() => ({})); throw new Error(detail.error || 'File operation failed.'); }
  return response;
}
export const files = {
  roots: async (): Promise<SharedFolder[]> => (await checked('/files/roots')).json(),
  list: async (id: string, path = ''): Promise<{ entries: { name: string; kind: 'file' | 'directory' | 'link' | 'other' }[]; truncated: boolean }> => (await checked(`/files/${encodeURIComponent(id)}/list?path=${encodeURIComponent(path)}`)).json(),
  read: async (id: string, path: string): Promise<ArrayBuffer> => (await checked(`/files/${encodeURIComponent(id)}/read?path=${encodeURIComponent(path)}`)).arrayBuffer(),
  readText: async (id: string, path: string): Promise<string> => (await checked(`/files/${encodeURIComponent(id)}/read?path=${encodeURIComponent(path)}`)).text(),
  writeText: async (id: string, path: string, content: string, expectedSha256: string): Promise<{ sha256: string }> => (await checked(`/files/${encodeURIComponent(id)}/write`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path, content, expectedSha256 }) })).json(),
};
