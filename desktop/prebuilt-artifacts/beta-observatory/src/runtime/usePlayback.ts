import { useEffect, useState } from 'react';
import type { Run } from './types';
import { eventDelay } from './trace';
import { followPlayback, initialPlayback, seekPlayback, synchronizePlayback } from './playbackState';

export function usePlayback(run: Run, live = !run.sample && (run.status === 'running' || run.status === 'awaiting-input')) {
  const [state, setState] = useState(() => initialPlayback(run, live));
  const [speed, setSpeed] = useState(1);
  const [skipIdle, setSkipIdle] = useState(true);
  // Derive the live cursor during render: a new batch or run cannot display an old step.
  const playback = synchronizePlayback(state, run, live);
  const { cursor, playing, following } = playback;
  useEffect(() => { setState(current => synchronizePlayback(current, run, live)); }, [run.id, run.events.length, live]);
  useEffect(() => {
    if (!playing) return;
    const timer = setTimeout(() => setState(current => {
      if (current.runId !== run.id) return current;
      return { ...current, cursor: Math.min(current.cursor + 1, Math.max(0, run.events.length - 1)) };
    }), eventDelay(run.events, cursor, speed, skipIdle));
    return () => clearTimeout(timer);
  }, [playing, cursor, speed, skipIdle, run.events, run.id]);
  function seek(index: number) { setState(current => seekPlayback(current, run, live, index)); }
  function toggle() {
    setState(current => {
      const resolved = synchronizePlayback(current, run, live);
      return { ...resolved, cursor: resolved.cursor >= run.events.length - 1 ? 0 : resolved.cursor, following: false, playing: !resolved.playing };
    });
  }
  function follow() { setState(current => followPlayback(current, run, live)); }
  return { cursor, playing, speed, skipIdle, following, live, event: run.events[cursor], setSpeed, setSkipIdle, seek, toggle, follow };
}
