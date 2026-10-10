import { describe, expect, it } from 'vitest';
import { pollState } from './ListenerPanel';
import type { RunEvent } from './types';

const event = (seq: number, type: string, data: Record<string, unknown>): RunEvent => ({
  seq,
  session_id: 's',
  epoch: 1,
  type,
  timestamp: '',
  data,
});
const snapshot = (seq: number, id: string, flags: string[]) =>
  event(seq, 'snapshot', { snapshot: { event: { id, flags } } });
const poll = event(5, 'poll.open', {
  event_id: '148',
  question: 'Do they convert?',
  down: 3,
  distance: 11,
});

describe('third-down poll', () => {
  it('stays open until the play itself is shown, then reports the outcome', () => {
    expect(pollState([])).toBeNull();
    const open = pollState([snapshot(4, '120', ['first_down']), poll]);
    expect(open).toMatchObject({ eventId: '148', down: 3, distance: 11, closed: false });
    const converted = pollState([poll, snapshot(9, '148', ['first_down'])]);
    expect(converted).toMatchObject({ closed: true, outcome: 'convert' });
  });
  it('never counts a reversed or flagged conversion', () => {
    for (const flags of [['first_down', 'reversed'], ['touchdown', 'no_play'], ['incomplete_pass']])
      expect(pollState([poll, snapshot(9, '148', flags)])?.outcome).toBe('stopped');
  });
});
