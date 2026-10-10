import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Presenter } from './presenter';
import type { RunEvent } from './types';

const event = (seq: number, type: string, after?: string): RunEvent => ({
  seq,
  session_id: 's',
  epoch: 1,
  type,
  timestamp: '',
  data: after ? { present_after: after } : {},
});

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe('presenting what has been heard', () => {
  it('holds the next result behind the line still playing, then shows it in order', () => {
    const playing = new Set(['call']),
      shown: number[] = [];
    const presenter = new Presenter(
      (segment) => playing.has(segment),
      (item) => shown.push(item.seq),
    );
    presenter.push(event(1, 'turn'));
    presenter.push(event(2, 'snapshot', 'call')); // next play's touchdown, prepared early
    presenter.push(event(3, 'trace')); // later events keep their order behind it
    expect(shown).toEqual([1]);
    playing.delete('call');
    presenter.drain();
    expect(shown).toEqual([1, 2, 3]);
  });
  it('shows recovered history at once and applies superseded events silently', () => {
    const shown: [number, boolean][] = [];
    const presenter = new Presenter(
      () => true,
      (item, silent) => shown.push([item.seq, silent]),
    );
    presenter.push(event(1, 'turn', 'old'), true);
    presenter.push(event(2, 'turn', 'old'));
    expect(presenter.held).toBe(1);
    presenter.flush(); // pause: state updates, but no voice or caption
    expect(shown).toEqual([
      [1, false],
      [2, true],
    ]);
  });
  it('never freezes the screen behind a stalled device', () => {
    const shown: number[] = [];
    const presenter = new Presenter(
      () => true,
      (item) => shown.push(item.seq),
      3000,
    );
    presenter.push(event(1, 'snapshot', 'stuck'));
    vi.advanceTimersByTime(2000);
    expect(shown).toEqual([]);
    vi.advanceTimersByTime(1500);
    expect(shown).toEqual([1]);
  });
});
