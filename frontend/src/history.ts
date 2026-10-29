export type SearchHistory = { id: string; query: string; search: string; time: string };
export type ReadingHistory = { id: string; title: string; authors: string[]; page: number; time: string };
type Histories = { searches: SearchHistory; readings: ReadingHistory };
const memory = new Map<string, string>();

export function readHistory<K extends keyof Histories>(kind: K): Histories[K][] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(`paperbox:${kind}`) || memory.get(kind) || '[]');
    if (!Array.isArray(value)) return [];
    return value.filter((entry) => entry && typeof entry.id === 'string' && typeof entry.time === 'string' &&
      (kind === 'searches' ? typeof entry.query === 'string' && typeof entry.search === 'string' : typeof entry.title === 'string' && Array.isArray(entry.authors) && Number.isInteger(entry.page)));
  } catch { return []; }
}

export function writeHistory<K extends keyof Histories>(kind: K, values: Histories[K][]) {
  const json = JSON.stringify(values.slice(0, kind === 'searches' ? 50 : 30));
  memory.set(kind, json);
  try { localStorage.setItem(`paperbox:${kind}`, json); } catch { /* Private browsing or storage quota: the current tab remains usable. */ }
}

export function remember<K extends keyof Histories>(kind: K, entry: Histories[K]) {
  writeHistory(kind, [entry, ...readHistory(kind).filter((item) => item.id !== entry.id)]);
}
