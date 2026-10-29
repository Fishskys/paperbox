import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { readHistory, remember, writeHistory } from '../src/history';

beforeEach(() => {
  const values = new Map<string, string>();
  vi.stubGlobal('localStorage', { getItem: (key: string) => values.get(key) ?? '[]', setItem: (key: string, value: string) => values.set(key, value) });
});
afterEach(() => vi.unstubAllGlobals());

it('keeps the latest page once per paper and supports clearing', () => {
  remember('readings', { id: 'paper', title: 'Paper', authors: [], page: 2, time: '2026-10-29' });
  remember('readings', { id: 'paper', title: 'Paper', authors: [], page: 7, time: '2026-10-29' });
  expect(readHistory('readings')).toHaveLength(1);
  expect(readHistory('readings')[0].page).toBe(7);
  writeHistory('readings', []);
  expect(readHistory('readings')).toEqual([]);
});

it('bounds reading and search history independently', () => {
  for (let i = 0; i < 60; i++) {
    remember('readings', { id: String(i), title: 'Paper', authors: [], page: 1, time: '2026-10-29' });
    remember('searches', { id: String(i), query: 'clock', search: `q=clock&n=${i}`, time: '2026-10-29' });
  }
  expect(readHistory('readings')).toHaveLength(30);
  expect(readHistory('searches')).toHaveLength(50);
});

it('ignores malformed stored records', () => {
  localStorage.setItem('paperbox:readings', JSON.stringify([{ id: 'bad' }]));
  expect(readHistory('readings')).toEqual([]);
  localStorage.setItem('paperbox:searches', '{broken');
  expect(readHistory('searches')).toEqual([]);
});