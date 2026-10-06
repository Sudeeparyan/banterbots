import type { RunEvent, Session, Snapshot, Turn, Feed } from './types';
export function reduceEvent(session: Session, event: RunEvent): Session {
  if (event.session_id !== session.id || event.epoch < session.epoch) return session;
  // The trace window is bounded; its contents cannot deduplicate old events.
  // A cursor also prevents delayed snapshots or state from rolling a run back.
  if (event.seq <= (session.seq ?? 0)) return session;
  let next = event.epoch > session.epoch ? { ...session, epoch: event.epoch } : session;
  if (next.events.some((e) => e.seq === event.seq)) return next;
  next = {
    ...next,
    events: [...next.events.slice(-499), event],
    seq: Math.max(next.seq ?? 0, event.seq),
  };
  if (event.type === 'snapshot') next.snapshot = event.data.snapshot as Snapshot;
  if (event.type === 'turn') {
    const turn = event.data.turn as Turn;
    if (turn && !next.turns.some((t) => t.id === turn.id)) next.turns = [...next.turns, turn];
  }
  if (event.type === 'state')
    next = {
      ...next,
      ...event.data,
      id: next.id,
      epoch: event.epoch,
      events: next.events,
      seq: next.seq,
    } as Session;
  if (event.type === 'feed') next.feed = { ...next.feed, ...event.data } as Feed;
  if (event.type === 'metrics')
    next.metrics = { ...next.metrics, ...event.data } as Session['metrics'];
  return next;
}
export function mergeSessionView(current: Session | null, incoming: Session): Session {
  if (!current || current.id !== incoming.id) return incoming;
  if (incoming.epoch < current.epoch) return current;
  if (incoming.epoch === current.epoch && (incoming.seq ?? 0) < (current.seq ?? 0)) return current;
  return incoming;
}
