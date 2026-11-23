import { useEffect, useState } from 'react';

export function useModelUpdateAge(receivedAt?: number): number {
  const [now, setNow] = useState(Date.now);
  const hasUpdate = receivedAt !== undefined;

  useEffect(() => {
    if (!hasUpdate) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [hasUpdate]);

  return receivedAt === undefined ? 0 : Math.max(0, Math.floor((now - receivedAt) / 1000));
}
