import { atoms, atomById, connections } from '../catalog';
import type { NodeId, RunEvent } from '../runtime/types';
import { mapEvent } from '../runtime/trace';
const width = 174, height = 78;
export function LoopMap({ selected, onSelect, event, visited }: { selected: NodeId; onSelect: (node: NodeId) => void; event?: RunEvent; visited: Set<NodeId> }) {
  const mapping = mapEvent(event);
  const active = event ? [mapping.primary, ...mapping.related] : [];
  function edgePath(from: NodeId, to: NodeId, feedback?: boolean) {
    const a = atomById[from], b = atomById[to];
    if (a.y === b.y && !feedback) {
      const direction = b.x > a.x ? 1 : -1;
      const ax = a.x + (direction > 0 ? width : 0), bx = b.x + (direction > 0 ? 0 : width);
      return `M ${ax} ${a.y + 39} L ${bx} ${b.y + 39}`;
    }
    if (a.x === b.x) return `M ${a.x + 87} ${a.y + (b.y > a.y ? height : 0)} L ${b.x + 87} ${b.y + (b.y > a.y ? 0 : height)}`;
    if (feedback) {
      const ay = a.y + (b.y > a.y ? height : 0), by = b.y + (b.y > a.y ? 0 : height);
      const mid = (ay + by) / 2;
      return `M ${a.x + 120} ${ay} C ${a.x + 120} ${mid}, ${b.x + 120} ${mid}, ${b.x + 120} ${by}`;
    }
    return `M ${a.x + 60} ${a.y + height} C ${a.x + 60} ${a.y + height + 55}, ${b.x + 60} ${b.y - 55}, ${b.x + 60} ${b.y}`;
  }
  return <><p className="mobile-map-hint">Scroll across the map to explore all 13 nodes.</p><div className="map-canvas"><svg viewBox="0 0 900 715" role="group" aria-label="Interactive beta loop. Select a node to inspect its inputs and outputs.">
    <defs>
      <radialGradient id="ambient"><stop stopColor="#68e0d0" stopOpacity=".055" /><stop offset="1" stopColor="#68e0d0" stopOpacity="0" /></radialGradient>
      <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#647777" /></marker>
      <marker id="arrow-lit" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#78edcf" /></marker>
    </defs>
    <ellipse cx="440" cy="350" rx="460" ry="340" fill="url(#ambient)" />
    <text className="map-group" x="34" y="40">01 / ORCHESTRATION</text><text className="map-group" x="34" y="212">02 / THE WORKER LOOP</text><text className="map-group" x="34" y="384">03 / EVIDENCE & OUTCOME</text>
    {connections.map((edge, index) => {
      const connected = edge.from === selected || edge.to === selected;
      const lit = active.includes(edge.from) && [...active, ...mapping.inferred].includes(edge.to) || mapping.inferred.includes(edge.from) && active.includes(edge.to);
      return <path key={index} d={edgePath(edge.from, edge.to, edge.feedback)} className={`edge ${edge.feedback ? 'feedback' : ''} ${connected ? 'connected' : ''} ${lit ? 'lit' : ''}`} markerEnd={`url(#${lit ? 'arrow-lit' : 'arrow'})`}><title>{atomById[edge.from].name} → {atomById[edge.to].name}: {edge.label}</title></path>;
    })}
    {atoms.map(atom => {
      const lit = active.includes(atom.id), inferred = event && mapping.inferred.includes(atom.id);
      return <g key={atom.id} role="button" tabIndex={0} aria-label={`${atom.number}. ${atom.name}${lit ? ', active' : ''}`} aria-pressed={selected === atom.id} onClick={() => onSelect(atom.id)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(atom.id); } }} className={`graph-node ${lit ? 'active' : ''} ${inferred ? 'inferred' : ''} ${visited.has(atom.id) ? 'visited' : ''} ${selected === atom.id ? 'selected' : ''} ${lit && mapping.failed ? 'failed' : ''}`} transform={`translate(${atom.x},${atom.y})`}>
        <rect className="node-halo" x="-4" y="-4" width={width + 8} height={height + 8} rx="14" />
        <rect className="node-box" width={width} height={height} rx="10" />
        <text className="node-number" x="14" y="21">{atom.number}</text><circle className="node-dot" cx="155" cy="17" r="3" />
        <text className="node-name" x="14" y="43">{atom.name}</text><text className="node-short" x="14" y="62">{atom.short}</text>
      </g>;
    })}
    <text className="map-note" x="34" y="654">Evidence closes the loop.</text><text className="map-note muted" x="34" y="673">Evidence changes the next decision.</text>
  </svg></div></>;
}
