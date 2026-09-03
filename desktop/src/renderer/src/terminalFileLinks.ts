import type { Terminal, ILink } from '@xterm/xterm';
import { findFileReferences, resolveFileReference, type FileReference } from '../../shared/fileReferences';

/** Map UTF-16 text offsets to terminal cells, including wide characters and wrapped lines. */
export function terminalFileLinks(terminal: Terminal, lineNumber: number, root: string, open: (ref: FileReference) => void): ILink[] {
  const buffer = terminal.buffer.active;
  let first = lineNumber - 1;
  while (first > 0 && buffer.getLine(first)?.isWrapped) first--;
  let text = '';
  const cells: Array<{ x: number; y: number; width: number }> = [];
  for (let row = first; row < buffer.length; row++) {
    const line = buffer.getLine(row);
    if (!line || row > first && !line.isWrapped) break;
    for (let col = 0; col < line.length; col++) {
      const cell = line.getCell(col);
      if (!cell || cell.getWidth() === 0) continue;
      const chars = cell.getChars() || ' ';
      text += chars;
      for (let offset = 0; offset < chars.length; offset++) cells.push({ x: col + 1, y: row + 1, width: cell.getWidth() });
    }
  }
  return findFileReferences(text).flatMap((match) => {
    const ref = resolveFileReference(match.text, root);
    const start = cells[match.start], last = cells[match.end - 1];
    if (!ref || !start || !last) return [];
    return [{ text: match.text, range: { start: { x: start.x, y: start.y }, end: { x: last.x + last.width - 1, y: last.y } }, activate: () => open(ref) }];
  });
}
