import type { Event, WebContents, WebContentsWillFrameNavigateEventParams } from 'electron';

function origin(url: string): string {
  try { return new URL(url).origin; } catch { return ''; }
}

// Appended as an additional policy, so an artifact cannot relax the host boundary.
export function artifactFramePolicy(url: string): string {
  const http = origin(url), ws = http.replace('http:', 'ws:');
  return [
    "default-src 'none'",
    `script-src ${http} 'unsafe-inline' 'unsafe-eval' blob:`,
    `style-src ${http} 'unsafe-inline'`,
    `connect-src ${http} ${ws}`,
    `img-src ${http} data: blob:`,
    `font-src ${http} data: blob:`,
    `media-src ${http} data: blob:`,
    `worker-src ${http} blob:`,
    "frame-src 'none'", "object-src 'none'", "base-uri 'self'", "form-action 'self'",
    'sandbox allow-scripts allow-same-origin allow-forms',
  ].join('; ');
}

/** One workbench window owns this session's request handlers. Dispose before reopening it. */
export function installArtifactFrameSecurity(contents: WebContents, previewOrigins: () => string[]): () => void {
  const session = contents.session;
  const frames = new Map<number, string>();
  const allowed = (url: string): boolean => /^http:\/\/127\.0\.0\.1:\d+$/.test(url) && previewOrigins().includes(url);
  const ownRequest = (url: string, owner: string): boolean => {
    const target = origin(url);
    return allowed(owner) && (target === owner || target === owner.replace('http:', 'ws:') || url.startsWith('data:'));
  };
  session.webRequest.onBeforeRequest((details, callback) => {
    if (details.webContentsId !== contents.id) { callback({}); return; }
    const frame = details.frame;
    if (details.resourceType === 'subFrame') {
      const target = origin(details.url);
      // Only a workbench-created, direct child can acquire a live artifact origin.
      const owner = frame && frames.get(frame.frameTreeNodeId);
      const permit = frame?.parent === contents.mainFrame && allowed(target) && (!owner || owner === target);
      if (permit && frame) frames.set(frame.frameTreeNodeId, target);
      callback({ cancel: !permit }); return;
    }
    if (frame && frame !== contents.mainFrame) {
      callback({ cancel: !ownRequest(details.url, frames.get(frame.frameTreeNodeId) || '') }); return;
    }
    callback({});
  });
  // Service workers could survive a preview and intercept a later server using its port.
  // Ordinary web workers remain available to compute/layout-heavy visualizations.
  session.webRequest.onBeforeSendHeaders((details, callback) => {
    callback({ cancel: Object.keys(details.requestHeaders).some(key => key.toLowerCase() === 'service-worker') });
  });
  session.webRequest.onHeadersReceived((details, callback) => {
    // Worker requests can lack webContentsId; constrain every response from an owned origin.
    if (!allowed(origin(details.url))) { callback({}); return; }
    const headers = { ...details.responseHeaders };
    const key = Object.keys(headers).find(key => key.toLowerCase() === 'content-security-policy') || 'Content-Security-Policy';
    headers[key] = [...(headers[key] || []), artifactFramePolicy(details.url)];
    // Parent policy and navigation guards apply even to older/generated templates.
    callback({ responseHeaders: headers });
  });
  const navigate = (event: Event<WebContentsWillFrameNavigateEventParams>): void => {
    if (event.isMainFrame) { event.preventDefault(); return; }
    const frame = event.frame, target = origin(event.url);
    const owner = frame && frames.get(frame.frameTreeNodeId);
    if (frame?.parent !== contents.mainFrame || !allowed(target) || (owner && owner !== target)) event.preventDefault();
  };
  contents.on('will-frame-navigate', navigate);
  contents.on('will-redirect', navigate);
  session.setPermissionCheckHandler((sender, permission, _origin, details) =>
    sender?.id === contents.id && details.isMainFrame && ['clipboard-read', 'clipboard-sanitized-write'].includes(permission));
  session.setPermissionRequestHandler((sender, permission, callback, details) =>
    callback(sender.id === contents.id && details.isMainFrame && ['clipboard-read', 'clipboard-sanitized-write'].includes(permission)));
  const download = (event: Event, _item: Electron.DownloadItem, sender: WebContents): void => {
    if (sender.id === contents.id) event.preventDefault();
  };
  session.on('will-download', download);
  return () => {
    session.webRequest.onBeforeRequest(null);
    session.webRequest.onBeforeSendHeaders(null);
    session.webRequest.onHeadersReceived(null);
    session.setPermissionCheckHandler(null);
    session.setPermissionRequestHandler(null);
    session.removeListener('will-download', download);
    contents.removeListener('will-frame-navigate', navigate);
    contents.removeListener('will-redirect', navigate);
    frames.clear();
  };
}
