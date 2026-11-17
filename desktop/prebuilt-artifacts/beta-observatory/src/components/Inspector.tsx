import { atomById, connections } from '../catalog';
import type { NodeId, RunEvent } from '../runtime/types';
import { mapEvent, timeLabel } from '../runtime/trace';
import { Icon } from './Icon';
export function Inspector({ selected, onSelect, event }: { selected: NodeId; onSelect: (node: NodeId) => void; event?: RunEvent }) {
  const atom = atomById[selected], mapped = mapEvent(event);
  return <aside className="inspector">
    <div className="inspector-heading"><span className="eyebrow">ATOM INSPECTOR</span><span className="mono">{atom.number} / 13</span></div>
    <div className="atom-symbol"><Icon name={selected === 'model' ? 'orbit' : selected === 'tools' ? 'terminal' : 'map'} size={24} /></div>
    <span className="eyebrow accent">{atom.group}</span><h2>{atom.name}</h2><p>{atom.description}</p>
    <section className="io-section"><h3><span className="io-dot" /> What goes in</h3>{atom.inputs.map(input => <div className="io-item" key={input}><span>↳</span>{input}</div>)}</section>
    <section className="io-section"><h3><span className="io-dot out" /> What comes out</h3>{atom.outputs.map(output => <div className="io-item" key={output}><span>↗</span>{output}</div>)}</section>
    <section className="contract"><span className="eyebrow">BEHAVIORAL CONTRACT</span><p>{atom.contract}</p></section>
    <section><h3>Connected to</h3><div className="connection-list">{connections.filter(edge => edge.from === selected).map(edge => <button key={edge.to} onClick={() => onSelect(edge.to)}><span>{atomById[edge.to].name}<small>{edge.label}</small></span><Icon name="arrow" size={15} /></button>)}</div></section>
    <div className="source-ref"><span className="eyebrow">IMPLEMENTATION</span><code>{atom.source}</code></div>
    {event && <section className="event-inspector"><div className="inspector-heading"><span className="eyebrow">SELECTED EVENT</span><span className="mono">{timeLabel(event.at)}</span></div><h3>{mapped.label}</h3><span className={`event-tag ${mapped.failed ? 'bad' : ''}`}>{mapped.failed ? 'Failure recorded' : `Observed · ${atomById[mapped.primary].name}`}</span>{mapped.inferred.length > 0 && <p className="hint">Inferred participation: {mapped.inferred.map(id => atomById[id].name).join(', ')}.</p>}<details><summary>Event payload <span className="mono">#{event.seq}</span></summary><pre>{JSON.stringify(event.data, null, 2)}</pre></details></section>}
  </aside>;
}
