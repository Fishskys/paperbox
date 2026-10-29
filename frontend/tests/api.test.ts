import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, request, setCredential } from '../src/api';

afterEach(() => { setCredential(''); vi.unstubAllGlobals(); });
describe('API boundary', () => {
  it('keeps credentials in request headers and preserves status errors', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'insufficient role' }), { status: 403 }));
    vi.stubGlobal('fetch', fetcher);
    setCredential('test-only');
    await expect(request('/api/papers')).rejects.toMatchObject({ status: 403 });
    expect(fetcher.mock.calls[0][1].headers.Authorization).toBe('Bearer test-only');
  });
  it('does not reinterpret a transport failure as an empty result', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('network')));
    await expect(request('/api/papers')).rejects.toThrow('无法连接');
  });
  it('retains retry-after from backpressure responses', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"detail":"busy"}', { status: 429, headers: { 'Retry-After': '5' } })));
    await expect(request('/api/papers')).rejects.toEqual(new ApiError(429, 'busy', '5'));
  });
});
