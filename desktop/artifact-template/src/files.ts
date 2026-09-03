export interface SharedFolder { id: string; label: string; readOnly: true }
async function checked(url: string): Promise<Response> {
  const response = await fetch(url);
  if (!response.ok) { const detail = await response.json().catch(() => ({})); throw new Error(detail.error || 'File read failed.'); }
  return response;
}
export const files = {
  roots: async (): Promise<SharedFolder[]> => (await checked('/files/roots')).json(),
  list: async (id: string, path = ''): Promise<{ entries: { name: string; kind: 'file' | 'directory' | 'link' | 'other' }[]; truncated: boolean }> => (await checked(`/files/${encodeURIComponent(id)}/list?path=${encodeURIComponent(path)}`)).json(),
  read: async (id: string, path: string): Promise<ArrayBuffer> => (await checked(`/files/${encodeURIComponent(id)}/read?path=${encodeURIComponent(path)}`)).arrayBuffer(),
  readText: async (id: string, path: string): Promise<string> => (await checked(`/files/${encodeURIComponent(id)}/read?path=${encodeURIComponent(path)}`)).text(),
};
