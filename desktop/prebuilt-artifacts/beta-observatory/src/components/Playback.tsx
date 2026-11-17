import type { Run } from '../runtime/types';
import type { usePlayback } from '../runtime/usePlayback';
import { timeLabel, mapEvent } from '../runtime/trace';
import { Icon } from './Icon';
import { pendingDecision } from '../runtime/playbackState';
export function Playback({ run, playback, onFollowLive }: { run: Run; playback: ReturnType<typeof usePlayback>; onFollowLive: () => void }) {
  const { cursor, event, seek, playing, toggle, speed, setSpeed, skipIdle, setSkipIdle, live, following } = playback;
  return <section className="playback" aria-label="Run playback controls">
    <div className="playback-meta"><div className="run-caption"><span className={`signal ${live && following ? 'live' : ''}`} /><span>{run.sample ? 'SAMPLE RECORDING' : live ? 'LIVE RECORDING' : 'RECORDED RUN'}</span><span className="divider">/</span><span className="run-title">{run.prompt}</span></div><span className="mono">{Math.min(cursor + 1, run.events.length)} / {run.events.length} events</span></div>
    {!run.sample && <p className="playback-repository">Working repository: <strong>{run.repository?.label || 'Observatory artifact'}</strong>{run.repository && <span> · {run.repository.access === 'read' ? 'Read only' : 'Read & write'}</span>}</p>}
    {live && run.status === 'awaiting-input' && <div className="playback-decision" role="status"><span><strong>Awaiting your input</strong> · {pendingDecision(run)}</span><a href="#observation-decision">Review decision <Icon name="arrow" size={14} /></a></div>}
    <div className="timeline-track"><input aria-label="Replay event" type="range" min="0" max={Math.max(0, run.events.length - 1)} value={Math.min(cursor, Math.max(0, run.events.length - 1))} onChange={event => seek(Number(event.target.value))} disabled={!run.events.length} />
      <div className="event-markers" aria-hidden="true">{run.events.map((event, index) => <span key={event.seq} className={`${index <= cursor ? 'seen' : ''} ${mapEvent(event).failed ? 'failure' : ''}`} style={{ left: `${index / Math.max(1, run.events.length - 1) * 100}%` }} />)}</div>
    </div>
    <div className="transport"><div className="transport-buttons"><button aria-label="Restart replay" onClick={() => seek(0)} disabled={!run.events.length}><Icon name="reset" /></button><button aria-label="Previous event" onClick={() => seek(cursor - 1)} disabled={cursor <= 0}><Icon name="back" /></button><button className="play-button" aria-label={playing ? 'Pause replay' : 'Play replay'} onClick={toggle} disabled={run.events.length < 2 || live}><Icon name={playing ? 'pause' : 'play'} /></button><button aria-label="Next event" onClick={() => seek(cursor + 1)} disabled={cursor >= run.events.length - 1}><Icon name="next" /></button><span className="play-time mono">{timeLabel(event?.at || 0)} <span>/ {timeLabel(run.events.at(-1)?.at || 0)}</span></span></div>
      <div className="transport-options">{live && <button className={following ? 'active-text' : ''} aria-pressed={following} onClick={onFollowLive}><span className="signal live" />{following ? 'Following live' : 'Return to live'}</button>}<label className="skip-idle"><input type="checkbox" checked={skipIdle} onChange={event => setSkipIdle(event.target.checked)} /> Skip idle gaps</label><label className="speed-select"><span>Speed</span><select aria-label="Playback speed" value={speed} onChange={event => setSpeed(Number(event.target.value))}>{[0.25, 0.5, 1, 1.5, 2, 4, 8].map(value => <option key={value} value={value}>{value}×</option>)}</select></label></div>
    </div>
  </section>;
}
