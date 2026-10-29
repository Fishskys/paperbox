import { afterEach, expect, it, vi } from 'vitest';
import { ApiError } from '../src/api';
import type { BatchAccepted } from '../src/contracts';
import { retryDelay, UploadQueue } from '../src/upload-queue';

const file = (name = 'paper.pdf') => new File(['pdf'], name, { type: 'application/pdf' });
const accepted = (name = 'paper.pdf'): BatchAccepted => ({ accepted: 1, duplicate: 0, rejected: 0, results: [{ filename: name, status: 'accepted', job_id: 'job' }] });
afterEach(() => vi.useRealTimers());

it('limits in-flight requests to two and stopping leaves submitted requests alive', async () => {
  const finish: (() => void)[] = [];
  const sender = vi.fn((value: File) => new Promise<BatchAccepted>((resolve) => { finish.push(() => resolve(accepted(value.name))); }));
  const queue = new UploadQueue(sender);
  queue.add([file('a.pdf'), file('b.pdf'), file('c.pdf')]);
  queue.start();
  expect(sender).toHaveBeenCalledTimes(2);
  queue.stopQueued();
  expect(queue.snapshot()[2].state).toBe('stopped');
  finish.forEach((resolve) => resolve());
  await vi.waitFor(() => expect(queue.snapshot().filter((item) => item.state === 'accepted')).toHaveLength(2));
  expect(sender).toHaveBeenCalledTimes(2);
});

it('respects retry-after and stops after three rejected attempts', async () => {
  vi.useFakeTimers();
  const sender = vi.fn().mockRejectedValue(new ApiError(429, 'busy', '5'));
  const queue = new UploadQueue(sender);
  queue.add([file()]); queue.start();
  await vi.advanceTimersByTimeAsync(4999);
  expect(sender).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(sender).toHaveBeenCalledTimes(2);
  await vi.advanceTimersByTimeAsync(5000);
  expect(sender).toHaveBeenCalledTimes(3);
  expect(queue.snapshot()[0].state).toBe('failed');
});

it('does not retry ambiguous transport failures or permission errors', async () => {
  for (const failure of [new Error('network'), new ApiError(403, 'forbidden')]) {
    const sender = vi.fn().mockRejectedValue(failure);
    const queue = new UploadQueue(sender);
    queue.add([file()]); queue.start();
    await vi.waitFor(() => expect(queue.snapshot()[0].state).toBe('failed'));
    expect(sender).toHaveBeenCalledTimes(1);
  }
});

it('stopping during backoff prevents the next request', async () => {
  vi.useFakeTimers();
  const sender = vi.fn().mockRejectedValue(new ApiError(429, 'busy', '30'));
  const queue = new UploadQueue(sender);
  queue.add([file()]); queue.start();
  await vi.advanceTimersByTimeAsync(0);
  expect(queue.snapshot()[0].state).toBe('backoff');
  queue.stopQueued();
  await vi.runAllTimersAsync();
  expect(sender).toHaveBeenCalledTimes(1);
  expect(queue.snapshot()[0].state).toBe('stopped');
});

it('parses date retry headers and rejects unsupported files locally', () => {
  expect(retryDelay('Thu, 29 Oct 2026 00:00:05 GMT', 1, Date.parse('2026-10-29T00:00:00Z'))).toBe(5000);
  expect(retryDelay('invalid', 10)).toBe(30000);
  const queue = new UploadQueue(); queue.add([file('notes.txt')]);
  expect(queue.snapshot()[0].state).toBe('failed');
});