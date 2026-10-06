import { describe, expect, it } from 'vitest';
import { exchangesFor, nflDate, transcriptFor } from './broadcast';
import type { Session, Snapshot, Turn } from './types';

const snapshot = (hash: string, score: number) =>
  ({
    hash,
    event: {
      id: 'same-play',
      quarter: 4,
      clock: '00:05',
      away_team: 'BAL',
      home_team: 'KC',
      away_score: score,
      home_score: 27,
      description: score ? 'Touchdown.' : 'Review: incomplete.',
    },
  }) as Snapshot;
const turn = (id: string, exchange: string, hash: string, text: string) =>
  ({
    id,
    exchange_id: exchange,
    snapshot_hash: hash,
    event_id: 'same-play',
    persona: 'Riley Brooks',
    team_id: 'BAL',
    text,
    spoken_text: null,
  }) as Turn;
const run = {
  id: 'saved-run',
  mode: 'replay',
  provider: 'demo',
  game: { home_team: 'KC', away_team: 'BAL' },
  events: [
    { type: 'snapshot', data: { snapshot: snapshot('original', 26) } },
    { type: 'snapshot', data: { snapshot: snapshot('corrected', 20) } },
  ],
  snapshot: snapshot('corrected', 20),
  turns: [
    turn('1', 'exchange-1', 'original', 'Touchdown.'),
    turn('2', 'exchange-1', 'original', 'Reply.'),
    turn('3', 'exchange-2', 'corrected', 'Ruling changed.'),
  ],
} as unknown as Session;

describe('broadcast conversation', () => {
  it('keeps the NFL scoreboard on the Eastern game date across midnight in Europe', () => {
    expect(nflDate(new Date('2026-10-07T00:30:00Z'))).toBe('2026-10-06');
    expect(nflDate(new Date('2026-10-07T12:30:00Z'))).toBe('2026-10-07');
  });
  it('groups a call and reply with their own frozen revision instead of the latest score', () => {
    const groups = exchangesFor(run);
    expect(groups.map((group) => group.turns.length)).toEqual([2, 1]);
    expect(groups.map((group) => group.snapshot?.event.away_score)).toEqual([26, 20]);
  });
  it('exports actual observed speech, source attribution and both revision contexts in order', () => {
    const spoken = {
      ...run,
      turns: [{ ...run.turns[0], spoken_text: 'Observed voice.' }, ...run.turns.slice(1)],
    };
    const text = transcriptFor(spoken);
    expect(text).toContain('Historical replay · Demo commentary');
    expect(text).toContain('BAL 26–27 KC');
    expect(text).toContain('BAL 20–27 KC');
    expect(text).toContain('Riley Brooks (BAL): Observed voice.');
    expect(text.indexOf('Observed voice.')).toBeLessThan(text.indexOf('Ruling changed.'));
  });
  it('retains recorded turns even when old snapshot traces are no longer loaded', () => {
    const groups = exchangesFor({ ...run, events: [] });
    expect(groups).toHaveLength(2);
    expect(groups[0].snapshot).toBeUndefined();
    expect(groups[0].turns).toHaveLength(2);
    expect(exchangesFor(null)).toEqual([]);
  });
});
