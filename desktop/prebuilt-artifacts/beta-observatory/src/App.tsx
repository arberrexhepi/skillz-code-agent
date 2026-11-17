import { useEffect, useMemo, useRef, useState } from 'react';
import { atoms, atomById, connections } from './catalog';
import { LoopMap } from './components/LoopMap';
import { Inspector } from './components/Inspector';
import { Playback } from './components/Playback';
import { RunConsole } from './components/RunConsole';
import { RunDecisionPanel } from './components/RunDecisionPanel';
import { Icon } from './components/Icon';
import { useRuntime } from './runtime/useRuntime';
import { usePlayback } from './runtime/usePlayback';
import { mapEvent, timeLabel } from './runtime/trace';
import { sampleRun } from './runtime/sample';
import type { NodeId, Run } from './runtime/types';
function download(run: Run) {
  const blob = new Blob([JSON.stringify({ format: 'skillz-beta-trace', version: 1, ...run }, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = `beta-trace-${run.id}.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export default function App() {
  const runtime = useRuntime();
  const live = runtime.status.activeRunId === runtime.run.id && (runtime.run.status === 'running' || runtime.run.status === 'awaiting-input');
  const playback = usePlayback(runtime.run, live);
  const [view, setView] = useState<'map' | 'anatomy' | 'history'>('map');
  const [selected, setSelected] = useState<NodeId>('model');
  const [autoInspect, setAutoInspect] = useState(true);
  const [decisionReply, setDecisionReply] = useState('');
  const trace = useRef<HTMLDivElement>(null);
  useEffect(() => { setAutoInspect(true); setDecisionReply(''); }, [runtime.run.id]);
  useEffect(() => {
    if (!playback.following || !playback.live) return;
    const strip = trace.current;
    const current = strip?.querySelector<HTMLButtonElement>('button.current');
    if (strip && current) strip.scrollLeft = current.offsetLeft - strip.offsetLeft - strip.clientWidth + current.offsetWidth + 22;
  }, [playback.cursor, playback.live, playback.following]);
  const visited = useMemo(() => new Set(runtime.run.events.slice(0, playback.cursor + 1).flatMap(event => { const mapped = mapEvent(event); return [mapped.primary, ...mapped.related]; })), [runtime.run.events, playback.cursor]);
  const mapping = mapEvent(playback.event);
  const selectedNode = autoInspect && playback.event ? mapping.primary : selected;
  function selectNode(id: NodeId) { setSelected(id); setAutoInspect(false); }
  return <div className="observatory">
    <header className="masthead"><a className="brand" href="#" onClick={event => { event.preventDefault(); setView('map'); }}><span className="brand-symbol"><Icon name="orbit" size={28} /></span><span>skillz<span className="brand-separator">/</span><strong>Agent Observatory</strong></span><span className="beta-tag">BETA RUNTIME</span></a><div className="header-right"><span className="edition mono">TREELOOP / 01</span><span className="header-status"><span className={`signal ${runtime.status.available ? 'live' : ''}`} />{runtime.status.available ? runtime.status.ready === false ? 'Runtime needs setup' : 'Runtime connected' : 'Explore mode'}</span></div></header>
    <main><section className="intro"><div><span className="eyebrow accent">AN OPEN VIEW INTO AGENT EXECUTION</span><h1>Inside the loop<span>.</span></h1><p>Every decision has an input. Every action leaves evidence. <br />Explore the beta, one component at a time — or watch it work as a whole.</p></div><div className="intro-stats"><div><strong>13</strong><span>atomic parts</span></div><div><strong>{connections.length}</strong><span>connections</span></div><div><strong>{runtime.run.events.length}</strong><span>recorded events</span></div></div></section>
      <div className="section-nav"><nav aria-label="Showcase views">{([{ id: 'map', icon: 'map', label: 'System map' }, { id: 'anatomy', icon: 'grid', label: 'Loop anatomy' }, { id: 'history', icon: 'history', label: 'Run archive' }] as const).map(tab => <button className={view === tab.id ? 'selected' : ''} aria-pressed={view === tab.id} key={tab.id} onClick={() => setView(tab.id)}><Icon name={tab.icon} size={16} />{tab.label}{tab.id === 'history' && <span className="count">{runtime.status.runs.length}</span>}</button>)}</nav><a className="launch-link" href="#run-console">Launch a run <Icon name="arrow" size={15} /></a></div>
      <div className="showcase-layout"><section className="stage">
        <div className="stage-top"><div><span className="eyebrow">{view === 'map' ? 'THE CONNECTED SYSTEM' : view === 'anatomy' ? 'SMALL PARTS. EXPLICIT CONTRACTS.' : 'CAPTURED EXECUTION'}</span><h2>{view === 'map' ? 'Intent → evidence → outcome' : view === 'anatomy' ? 'Anatomy of an agent' : 'Every run tells a story'}</h2></div><div className="stage-context">{!runtime.run.sample && runtime.run.repository && <span className="stage-repository" title={runtime.run.repository.label}>{runtime.run.repository.label}<small>{runtime.run.repository.access === 'read' ? 'Read only' : 'Read & write'}</small></span>}<span className="stage-version mono">main_v2.py</span></div></div>
        <RunDecisionPanel runtime={runtime} location="observation" reply={decisionReply} onReplyChange={setDecisionReply} />
        {view === 'map' && <><LoopMap selected={selectedNode} onSelect={selectNode} event={playback.event} visited={visited} /><div className="map-legend"><span><i className="legend-dot observed" />Observed event</span><span><i className="legend-dot inferred" />Inferred phase</span><span><i className="legend-line" />Feedback path</span><label><input type="checkbox" checked={autoInspect} onChange={event => setAutoInspect(event.target.checked)} /> Follow event in inspector</label></div></>}
        {view === 'anatomy' && <div className="anatomy-grid">{atoms.map(atom => <button key={atom.id} className={`atom-card ${selectedNode === atom.id ? 'selected' : ''}`} onClick={() => selectNode(atom.id)}><div><span className="mono accent">{atom.number}</span><span className="eyebrow">{atom.group}</span></div><h3>{atom.name}</h3><p>{atom.description}</p><div className="card-io"><span>IN <strong>{atom.inputs[0]}</strong></span><Icon name="arrow" size={14} /><span>OUT <strong>{atom.outputs[0]}</strong></span></div><span className="inspect-label">Inspect component <Icon name="arrow" size={14} /></span></button>)}</div>}
        {view === 'history' && <div className="archive"><div className="archive-intro"><Icon name="history" size={32} /><p>Real recordings are saved by the desktop. Open a run to inspect its events, replay its decisions, or export its trace.</p></div><button className="archive-row" onClick={() => { runtime.select('sample'); setView('map'); }}><span className="archive-icon"><Icon name="play" /></span><span><strong>A patch, a failure, a recovery.</strong><small>Illustrative sample · {sampleRun.events.length} events · no model usage</small></span><span className="sample-tag">SAMPLE</span><Icon name="chevron" size={16} /></button>{runtime.status.runs.map(run => <button className="archive-row" key={run.id} onClick={() => { runtime.select(run.id); setView('map'); }}><span className="archive-icon"><Icon name={run.status === 'completed' ? 'check' : 'history'} /></span><span><strong>{run.prompt}</strong><small>{new Date(run.startedAt).toLocaleString()} · {run.eventCount} events · {run.repository?.label || 'Observatory artifact'} · {run.selection.model}</small></span><span className={`status-pill ${run.status}`}>{run.status.replace('-', ' ')}</span><Icon name="chevron" size={16} /></button>)}{!runtime.status.runs.length && <p className="empty-history">Your first live run will appear here. Start one below.</p>}<p className="hint">The latest 20 recordings are retained. Export any trace you want to keep. Interrupted and failed runs remain inspectable.</p></div>}
        <Playback run={runtime.run} playback={playback} onFollowLive={() => { playback.follow(); setAutoInspect(true); }} />
        <div className="event-strip"><div><span className={`event-indicator ${mapping.failed ? 'failed' : ''}`} /><span className="mono">{timeLabel(playback.event?.at || 0)}</span><strong>{atomById[mapping.primary].name}</strong><span className="strip-summary">{mapping.label}</span></div><button aria-label="Export selected trace" title="Export selected trace as JSON" onClick={() => download(runtime.run)} disabled={!runtime.run.events.length}><Icon name="download" size={16} /></button></div>
        <div ref={trace} className="trace-events" aria-label="Recorded events">{runtime.run.events.map((event, index) => <button key={event.seq} className={`${index === playback.cursor ? 'current' : ''} ${mapEvent(event).failed ? 'failed' : ''}`} onClick={() => { playback.seek(index); selectNode(mapEvent(event).primary); }} title={mapEvent(event).label}><span>{String(index + 1).padStart(2, '0')}</span>{atomById[mapEvent(event).primary].name}</button>)}</div>
      </section><Inspector selected={selectedNode} onSelect={selectNode} event={playback.event} /></div>
      {(runtime.error || runtime.status.persistenceError) && <div className="error-banner" role="alert">{runtime.error || runtime.status.persistenceError}</div>}
      <RunConsole runtime={runtime} reply={decisionReply} onReplyChange={setDecisionReply} />
      <footer><span><Icon name="orbit" size={16} /> SKILLZ / AGENT OBSERVATORY</span><p>{runtime.run.sample ? 'Sample is illustrative. Live nodes are driven by recorded bridge events.' : 'Recorded bridge events. Inferred phases are labeled. Replay never executes tools.'}</p><span className="mono">BUILT TO BE UNDERSTOOD</span></footer>
    </main>
  </div>;
}
