import type { RunEvent, Session, Snapshot, Turn } from './types';

export type Exchange = {
  id: string;
  turns: Turn[];
  snapshot?: Snapshot;
};

export type AgentId = 'a' | 'b';
export type Party = 'studio' | AgentId;
export type StepState = 'waiting' | 'active' | 'done' | 'failed';
export type FlowStep = {
  key: 'deliver' | 'call' | 'ask' | 'reply' | 'return';
  from: Party;
  to: Party;
  state: StepState;
  taskId?: string;
  latencyMs?: number;
};
export type AgentFlow = {
  exchangeId: string;
  lead: AgentId;
  peer: AgentId;
  steps: FlowStep[];
  status: 'running' | 'done' | 'failed' | 'stopped';
  /** Rebuilt from saved turns because the protocol events are no longer available. */
  recorded: boolean;
};
export type Handoff = { latencyMs?: number; taskId?: string };

const other = (agent: AgentId): AgentId => (agent === 'a' ? 'b' : 'a');
const failedStates = new Set(['failed', 'canceled', 'rejected']);
const isA2A = (event: RunEvent) =>
  event.type === 'trace' && String(event.data.node ?? '').startsWith('a2a.');

/** Summarize one A2A request: the studio's call to the lead, or the lead's call to its peer. */
function hop(events: RunEvent[]) {
  const statuses = events.filter((e) => e.data.node === 'a2a.status');
  const receive = events.find((e) => e.data.node === 'a2a.receive');
  const states = statuses.map((e) => String(e.data.state));
  // The sender's own task ID is stamped on a2a.send; the remote task appears from status onward.
  const remote = [...statuses, ...(receive ? [receive] : [])].find((e) => e.data.task_id);
  return {
    accepted: statuses.length > 0,
    completed: Boolean(receive) || states.includes('completed'),
    failed: states.some((state) => failedStates.has(state)),
    taskId: remote ? String(remote.data.task_id) : undefined,
    latencyMs: typeof receive?.data.latency_ms === 'number' ? receive.data.latency_ms : undefined,
  };
}

/**
 * Explain the latest exchange as five plain steps: studio to lead agent, lead
 * writes its call, lead to peer over A2A, peer replies, lead back to studio.
 */
export function agentFlowFor(session: Session | null): AgentFlow | null {
  if (!session) return null;
  const events = session.events.filter((e) => e.seq >= (session.history_start_seq ?? 0));
  const start = events
    .map((e) => e.type === 'trace' && e.data.node === 'choose_lead')
    .lastIndexOf(true);
  if (start < 0) return recordedFlow(session);
  const recent = events.slice(start);
  const lead: AgentId = recent[0].data.lead === 'b' ? 'b' : 'a';
  const peer = other(lead);
  const exchangeId = String(recent[0].data.exchange_id ?? '');
  const turnBy = (agent: AgentId) =>
    session.turns.find((t) => t.exchange_id === exchangeId && t.agent_id === agent) ??
    recent.find((e) => e.type === 'turn' && (e.data.turn as Turn)?.agent_id === agent);
  const studio = hop(recent.filter((e) => isA2A(e) && !e.data.agent_id));
  const relay = hop(recent.filter((e) => isA2A(e) && e.data.agent_id === lead));
  const heardFrom = (agent: AgentId) => recent.some((e) => e.data.agent_id === agent);
  const returned =
    studio.completed ||
    recent.some(
      (e) =>
        e.type === 'trace' &&
        e.data.phase === 'complete' &&
        (e.data.node === 'a2a_exchange' || e.data.node === 'finalize'),
    );
  const done = [
    studio.accepted || heardFrom(lead),
    Boolean(turnBy(lead)),
    relay.accepted || heardFrom(peer),
    Boolean(turnBy(peer)),
    returned,
  ];
  const failed =
    studio.failed ||
    relay.failed ||
    recent.some(
      (e) => e.type === 'error' || (e.type === 'trace' && e.data.phase === 'interrupted'),
    );
  const complete = done.every(Boolean);
  // Pausing or restarting bumps the epoch and drops the old exchange's remaining events.
  const stopped = !complete && !failed && recent[recent.length - 1].epoch < session.epoch;
  const status = complete ? 'done' : failed ? 'failed' : stopped ? 'stopped' : 'running';
  const firstOpen = done.indexOf(false);
  const shape: Omit<FlowStep, 'state'>[] = [
    { key: 'deliver', from: 'studio', to: lead, taskId: studio.taskId },
    { key: 'call', from: lead, to: lead },
    { key: 'ask', from: lead, to: peer, taskId: relay.taskId },
    { key: 'reply', from: peer, to: lead, latencyMs: relay.latencyMs },
    { key: 'return', from: lead, to: 'studio', latencyMs: studio.latencyMs },
  ];
  const steps = shape.map((step, i): FlowStep => {
    const state: StepState = done[i]
      ? 'done'
      : i !== firstOpen || status === 'stopped'
        ? 'waiting'
        : status === 'failed'
          ? 'failed'
          : 'active';
    return { ...step, state };
  });
  return { exchangeId, lead, peer, steps, status, recorded: false };
}

function recordedFlow(session: Session): AgentFlow | null {
  const last = exchangesFor(session).at(-1);
  if (!last) return null;
  const lead = last.turns[0].agent_id;
  const peer = other(lead);
  const replied = last.turns.some((t) => t.agent_id === peer && t.reply_to_turn_id);
  const keys: FlowStep['key'][] = ['deliver', 'call', 'ask', 'reply', 'return'];
  const ends: [Party, Party][] = [
    ['studio', lead],
    [lead, lead],
    [lead, peer],
    [peer, lead],
    [lead, 'studio'],
  ];
  return {
    exchangeId: last.id,
    lead,
    peer,
    status: replied ? 'done' : 'stopped',
    recorded: true,
    steps: keys.map((key, i) => ({
      key,
      from: ends[i][0],
      to: ends[i][1],
      state: replied || i < 2 ? 'done' : 'waiting',
    })),
  };
}

/** Timing of each lead-to-peer A2A request, keyed by exchange. */
export function handoffsFor(session: Session | null): Map<string, Handoff> {
  const handoffs = new Map<string, Handoff>();
  for (const event of session?.events ?? []) {
    if (event.type !== 'trace' || event.data.node !== 'a2a.receive' || !event.data.agent_id)
      continue;
    handoffs.set(String(event.data.exchange_id), {
      latencyMs: typeof event.data.latency_ms === 'number' ? event.data.latency_ms : undefined,
      taskId: event.data.task_id ? String(event.data.task_id) : undefined,
    });
  }
  return handoffs;
}

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
  // A play's pre-snap setup is its own A2A exchange, but it shares the play's card.
  const setups = new Map<string, Exchange>();
  for (const turn of session.turns) {
    let group = groups.get(turn.exchange_id);
    if (!group) {
      const setup = turn.kind === 'setup' ? undefined : setups.get(turn.event_id);
      if (setup) {
        setups.delete(turn.event_id);
        groups.delete(setup.id);
        group = { ...setup, id: turn.exchange_id };
      } else group = { id: turn.exchange_id, turns: [] };
      groups.set(turn.exchange_id, group);
    }
    group.snapshot ??= snapshots.get(turn.snapshot_hash);
    group.turns.push(turn);
    if (turn.kind === 'setup') setups.set(turn.event_id, group);
  }
  return [...groups.values()];
}

const TOPICS: Record<string, string> = {
  intro: 'OPEN',
  quarter: 'QUARTER',
  halftime: 'HALFTIME',
  final: 'FINAL',
  drive_summary: 'DRIVE',
  listener_question: 'YOUR QUESTION',
};

/** The label a turn wears in the transcript: its role in the broadcast. */
export function turnLabel(turn: Turn): string {
  if (turn.kind === 'setup') return 'SETUP';
  if (turn.reply_to_turn_id) return 'REPLY';
  if (turn.kind === 'segment') return TOPICS[turn.topic ?? ''] ?? 'SEGMENT';
  return 'CALL';
}

export function transcriptFor(session: Session): string {
  const title = `${session.game.away_team} at ${session.game.home_team}`;
  const lines = [
    `BanterBots — ${title}`,
    `${session.mode === 'replay' ? 'Historical replay' : 'Live feed'} · ${session.provider === 'demo' ? 'Demo commentary' : 'Azure OpenAI commentary'}`,
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
