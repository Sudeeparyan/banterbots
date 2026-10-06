import type { Session, Snapshot, Turn } from './types';

export type Exchange = {
  id: string;
  turns: Turn[];
  snapshot?: Snapshot;
};

export function nflDate(now = new Date()): string {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(now);
  const value = (key: string) => parts.find((part) => part.type === key)?.value;
  return `${value('year')}-${value('month')}-${value('day')}`;
}

/** Match each conversation to its frozen facts, including revised versions of a play. */
export function exchangesFor(session: Session | null): Exchange[] {
  if (!session) return [];
  const snapshots = new Map<string, Snapshot>();
  for (const event of session.events) {
    if (event.type !== 'snapshot') continue;
    const snapshot = event.data.snapshot as Snapshot | undefined;
    if (snapshot?.hash) snapshots.set(snapshot.hash, snapshot);
  }
  if (session.snapshot) snapshots.set(session.snapshot.hash, session.snapshot);
  const groups = new Map<string, Exchange>();
  for (const turn of session.turns) {
    const group = groups.get(turn.exchange_id) ?? {
      id: turn.exchange_id,
      turns: [],
      snapshot: snapshots.get(turn.snapshot_hash),
    };
    group.turns.push(turn);
    groups.set(group.id, group);
  }
  return [...groups.values()];
}

export function transcriptFor(session: Session): string {
  const title = `${session.game.away_team} at ${session.game.home_team}`;
  const lines = [
    `BanterBots — ${title}`,
    `${session.mode === 'replay' ? 'Historical replay' : 'Live feed'} · ${session.provider === 'demo' ? 'Demo commentary' : 'OpenAI commentary'}`,
    `Run: ${session.id}`,
    '',
  ];
  for (const exchange of exchangesFor(session)) {
    const play = exchange.snapshot?.event;
    lines.push(
      play
        ? `Q${play.quarter} ${play.clock} · ${play.away_team} ${play.away_score}–${play.home_score} ${play.home_team}`
        : `Play ${exchange.turns[0].event_id}`,
    );
    if (play) lines.push(`Source play: ${play.description}`);
    for (const turn of exchange.turns)
      lines.push(`${turn.persona} (${turn.team_id}): ${turn.spoken_text ?? turn.text}`);
    lines.push('');
  }
  return lines.join('\n');
}
