export const DEFAULT_AUTO_MAX_TURNS = 3;
export const MIN_AUTO_MAX_TURNS = 1;
export const MAX_AUTO_MAX_TURNS = 25;

interface ComposerAgent {
  submit: (text: string) => Promise<boolean>;
  plannerAction: (action: string, extras?: Record<string, unknown>) => Promise<boolean>;
}

export function normalizeAutoMaxTurns(value: string | number): number {
  if (String(value).trim() === '') return DEFAULT_AUTO_MAX_TURNS;
  const turns = Math.floor(Number(value));
  if (!Number.isFinite(turns)) return DEFAULT_AUTO_MAX_TURNS;
  return Math.max(MIN_AUTO_MAX_TURNS, Math.min(MAX_AUTO_MAX_TURNS, turns));
}

export function submitComposerInstruction(
  agent: ComposerAgent,
  text: string,
  autoEnabled: boolean,
  maxTurns: string | number,
): Promise<boolean> {
  const prompt = text.trim();
  if (!prompt) return Promise.resolve(false);
  if (!autoEnabled) return agent.submit(prompt);
  return agent.plannerAction('start_continuous', {
    max_cycles: normalizeAutoMaxTurns(maxTurns),
    prompt,
  });
}

interface AgentComposerControlsProps {
  autoEnabled: boolean;
  maxTurns: string;
  disabled?: boolean;
  onAutoEnabledChange: (enabled: boolean) => void;
  onMaxTurnsChange: (value: string) => void;
}

export function AgentComposerControls({
  autoEnabled,
  maxTurns,
  disabled = false,
  onAutoEnabledChange,
  onMaxTurnsChange,
}: AgentComposerControlsProps): React.JSX.Element {
  return (
    <div className="composer-meta">
      <span className="composer-shortcuts"><span>↵ send</span><span>⇧↵ newline</span></span>
      <label className="composer-auto-toggle">
        <input
          type="checkbox"
          checked={autoEnabled}
          disabled={disabled}
          onChange={(event) => onAutoEnabledChange(event.target.checked)}
        />
        <span>Auto</span>
      </label>
      {autoEnabled && (
        <label className="composer-turn-limit">
          <span>Max turns</span>
          <input
            type="number"
            min={MIN_AUTO_MAX_TURNS}
            max={MAX_AUTO_MAX_TURNS}
            step={1}
            inputMode="numeric"
            aria-label="Maximum auto turns"
            value={maxTurns}
            disabled={disabled}
            onChange={(event) => onMaxTurnsChange(event.target.value)}
            onBlur={(event) => onMaxTurnsChange(String(normalizeAutoMaxTurns(event.target.value)))}
          />
        </label>
      )}
    </div>
  );
}
