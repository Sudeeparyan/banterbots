import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api, post } from './types';

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('bounded studio requests', () => {
  it('returns JSON and preserves Headers objects without replacing custom content types', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 'run' })));
    vi.stubGlobal('fetch', fetch);
    const headers = new Headers({
      Authorization: 'Bearer example',
      'Content-Type': 'application/problem+json',
    });
    await expect(api('/sessions', { headers })).resolves.toEqual({ id: 'run' });
    const sent = fetch.mock.calls[0][1] as RequestInit;
    expect(new Headers(sent.headers).get('Authorization')).toBe('Bearer example');
    expect(new Headers(sent.headers).get('Content-Type')).toBe('application/problem+json');
    expect(headers.get('Content-Type')).toBe('application/problem+json');
    expect(vi.getTimerCount()).toBe(0);
  });

  it('bounds a hanging fetch even when it never rejects after abort', async () => {
    const fetch = vi.fn().mockImplementation(() => new Promise(() => {}));
    vi.stubGlobal('fetch', fetch);
    const outcome = expect(api('/sessions')).rejects.toMatchObject({
      name: 'TimeoutError',
      message: expect.stringContaining('timed out'),
    });
    await vi.advanceTimersByTimeAsync(44999);
    const signal = (fetch.mock.calls[0][1] as RequestInit).signal!;
    expect(signal.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    await outcome;
    expect(signal.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('bounds a response whose headers arrive but JSON body stalls', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, json: () => new Promise(() => {}) }),
    );
    const outcome = expect(api('/games', undefined, 30000)).rejects.toMatchObject({
      name: 'TimeoutError',
    });
    await vi.advanceTimersByTimeAsync(30000);
    await outcome;
    expect(vi.getTimerCount()).toBe(0);
  });

  it('preserves caller cancellation and detaches its listener after completion', async () => {
    const caller = new AbortController(),
      reason = new DOMException('Game changed', 'AbortError');
    const add = vi.spyOn(caller.signal, 'addEventListener'),
      remove = vi.spyOn(caller.signal, 'removeEventListener');
    const fetch = vi.fn().mockImplementation(() => new Promise(() => {}));
    vi.stubGlobal('fetch', fetch);
    const outcome = expect(api('/games', { signal: caller.signal })).rejects.toBe(reason);
    const child = (fetch.mock.calls[0][1] as RequestInit).signal!;
    expect(child).not.toBe(caller.signal);
    caller.abort(reason);
    await outcome;
    expect(child.aborted).toBe(true);
    expect(child.reason).toBe(reason);
    expect(remove).toHaveBeenCalledWith('abort', add.mock.calls[0][1]);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('never dispatches a request that the caller already cancelled', async () => {
    const caller = new AbortController(),
      fetch = vi.fn();
    caller.abort(new DOMException('Cancelled', 'AbortError'));
    vi.stubGlobal('fetch', fetch);
    await expect(api('/games', { signal: caller.signal })).rejects.toMatchObject({
      name: 'AbortError',
    });
    expect(fetch).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('detaches caller cancellation after a successful response', async () => {
    const caller = new AbortController();
    const add = vi.spyOn(caller.signal, 'addEventListener');
    const remove = vi.spyOn(caller.signal, 'removeEventListener');
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ teams: [] })));
    vi.stubGlobal('fetch', fetch);
    await api('/teams', { signal: caller.signal });
    const child = (fetch.mock.calls[0][1] as RequestInit).signal!;
    expect(remove).toHaveBeenCalledWith('abort', add.mock.calls[0][1]);
    caller.abort();
    expect(child.aborted).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('keeps server error details and removes its request deadline', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ detail: 'Unknown replay game' }), { status: 400 }),
        ),
    );
    await expect(api('/sessions')).rejects.toThrow('Unknown replay game');
    expect(vi.getTimerCount()).toBe(0);
  });

  it('does not mislabel transport failures as timeouts', async () => {
    const error = new TypeError('Network unavailable');
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(error));
    await expect(api('/teams')).rejects.toBe(error);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('encodes POST bodies and supplies default JSON headers', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: 'paused' })));
    vi.stubGlobal('fetch', fetch);
    await expect(post('/sessions/run/control', { action: 'pause' })).resolves.toEqual({
      status: 'paused',
    });
    const [url, sent] = fetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/sessions/run/control');
    expect(sent.method).toBe('POST');
    expect(sent.body).toBe('{"action":"pause"}');
    expect(new Headers(sent.headers).get('Content-Type')).toBe('application/json');
  });
});
