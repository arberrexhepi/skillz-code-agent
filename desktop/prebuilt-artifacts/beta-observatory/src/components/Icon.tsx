export function Icon({ name, size = 18 }: { name: string; size?: number }) {
  const paths: Record<string, React.ReactNode> = {
    play: <path d="m9 5 11 7-11 7z" />, pause: <><path d="M8 5v14M16 5v14" /></>,
    back: <><path d="M5 5v14m13-14L7 12l11 7z" /></>, next: <path d="M19 5v14M6 5l11 7-11 7z" />,
    reset: <><path d="M3 4v6h6M4 10a8 8 0 1 1 1 8" /></>, arrow: <path d="M5 12h14m-6-6 6 6-6 6" />,
    map: <><rect x="3" y="3" width="6" height="6" rx="1" /><rect x="15" y="15" width="6" height="6" rx="1" /><path d="M6 9v9h9M9 6h9v9" /></>,
    grid: <><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></>,
    history: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>, download: <path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5" />,
    stop: <rect x="6" y="6" width="12" height="12" rx="1" />, terminal: <><path d="m4 6 6 6-6 6m9 0h7" /></>,
    orbit: <><circle cx="12" cy="12" r="3" /><ellipse cx="12" cy="12" rx="11" ry="6" transform="rotate(-35 12 12)" /></>,
    check: <path d="m5 12 4 4L19 6" />, chevron: <path d="m9 5 7 7-7 7" />,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.orbit}</svg>;
}
