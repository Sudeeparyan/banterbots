import { describe, expect, it } from 'vitest';
import { agentFlowFor, handoffsFor } from './broadcast';
import type { RunEvent, Session, Turn } from './types';

// Mirrors the event order the coordinator and agents emitted for a real demo exchange.
const LEAD_TASK = 'lead-task-1';
const PEER_TASK = 'peer-task-1';
const trace = (data: Record<string, unknown>) => ({ type: 'trace', data });
const turn = (agent_id: 'a' | 'b', reply_to_turn_id: string | null = null) =>
  ({
    id: `${agent_id}-turn`,
    exchange_id: 'x1',
    agent_id,
    reply_to_turn_id,
    persona: agent_id === 'a' ? 'Max Carter' : 'Riley Brooks',
  }) as Turn;
const script = [
  trace({ node: 'normalize', phase: 'complete' }),
  trace({ node: 'choose_lead', phase: 'complete', lead: 'a', exchange_id: 'x1' }),
  trace({ node: 'a2a_exchange', phase: 'start', from: 'coordinator', to: 'a' }),
  trace({ node: 'a2a.send', phase: 'start', target: 'http://127.0.0.1:8001', exchange_id: 'x1' }),
  trace({ node: 'a2a.status', state: 'submitted', task_id: LEAD_TASK, exchange_id: 'x1' }),
  trace({ agent_id: 'a', node: 'generate', phase: 'start', task_id: LEAD_TASK }),
  { type: 'turn', data: { turn: turn('a'), agent_id: 'a' } },
  trace({ agent_id: 'a', node: 'a2a.send', phase: 'start', task_id: LEAD_TASK, exchange_id: 'x1' }),
  trace({ agent_id: 'a', node: 'a2a.status', state: 'submitted', task_id: PEER_TASK }),
  trace({ agent_id: 'b', node: 'generate', phase: 'start', task_id: PEER_TASK }),
  { type: 'turn', data: { turn: turn('b', 'a-turn'), agent_id: 'b' } },
  trace({ agent_id: 'a', node: 'a2a.status', state: 'completed', task_id: PEER_TASK }),
  trace({
    agent_id: 'a',
    node: 'a2a.receive',
    phase: 'complete',
    task_id: PEER_TASK,
    exchange_id: 'x1',
    latency_ms: 615.6,
  }),
  trace({ node: 'a2a.status', state: 'completed', task_id: LEAD_TASK }),
  trace({ node: 'a2a.receive', phase: 'complete', task_id: LEAD_TASK, latency_ms: 1151.8 }),
  trace({ node: 'a2a_exchange', phase: 'complete' }),
  trace({ node: 'finalize', phase: 'complete' }),
];
const events = (count: number, epoch = 1): RunEvent[] =>
  script.slice(0, count).map((item, i) => ({
    ...item,
    seq: i + 1,
    epoch,
    session_id: 'run',
    timestamp: '2026-01-01T00:00:00Z',
  }));
const session = (count: number, extra: Partial<Session> = {}) => {
  const list = events(count);
  return {
    id: 'run',
    epoch: 1,
    history_start_seq: 0,
    events: list,
    turns: list.filter((e) => e.type === 'turn').map((e) => e.data.turn as Turn),
    ...extra,
  } as Session;
};
const states = (value: Session) => agentFlowFor(value)?.steps.map((step) => step.state);

describe('agent communication flow', () => {
  it('has nothing to explain before the first play', () => {
    expect(agentFlowFor(null)).toBeNull();
    expect(agentFlowFor(session(1))).toBeNull();
  });
  it('shows the studio handing the play to the lead first', () => {
    expect(states(session(3))).toEqual(['active', 'waiting', 'waiting', 'waiting', 'waiting']);
    expect(states(session(5))).toEqual(['done', 'active', 'waiting', 'waiting', 'waiting']);
  });
  it('tracks the lead asking its peer over A2A and the peer replying', () => {
    expect(states(session(8))).toEqual(['done', 'done', 'active', 'waiting', 'waiting']);
    expect(states(session(9))).toEqual(['done', 'done', 'done', 'active', 'waiting']);
    expect(states(session(11))).toEqual(['done', 'done', 'done', 'done', 'active']);
  });
  it('reports real task IDs, routing and timings once the exchange completes', () => {
    const flow = agentFlowFor(session(script.length))!;
    expect(flow.status).toBe('done');
    expect(flow.steps.map((step) => `${step.from}>${step.to}`)).toEqual([
      'studio>a',
      'a>a',
      'a>b',
      'b>a',
      'a>studio',
    ]);
    expect(flow.steps[0].taskId).toBe(LEAD_TASK);
    expect(flow.steps[2].taskId).toBe(PEER_TASK);
    expect(flow.steps[3].latencyMs).toBe(615.6);
    expect(flow.steps[4].latencyMs).toBe(1151.8);
  });
  it('routes through Riley when Riley leads', () => {
    const swapped = session(3);
    swapped.events = swapped.events.map((e) =>
      e.data.node === 'choose_lead' ? { ...e, data: { ...e.data, lead: 'b' } } : e,
    );
    const flow = agentFlowFor(swapped)!;
    expect([flow.lead, flow.peer]).toEqual(['b', 'a']);
    expect(flow.steps[2]).toMatchObject({ from: 'b', to: 'a' });
  });
  it('marks the current step failed when an agent reports an error', () => {
    const broken = session(9);
    broken.events = [
      ...broken.events,
      { ...broken.events[0], seq: 99, type: 'error', data: { message: 'peer down' } },
    ];
    expect(agentFlowFor(broken)?.status).toBe('failed');
    expect(states(broken)).toEqual(['done', 'done', 'done', 'failed', 'waiting']);
  });
  it('treats an exchange cut off by pause as stopped instead of still running', () => {
    const paused = session(8, { epoch: 2 });
    expect(agentFlowFor(paused)?.status).toBe('stopped');
    expect(states(paused)).not.toContain('active');
  });
  it('ignores exchanges from before a restart', () => {
    expect(agentFlowFor(session(script.length, { history_start_seq: 99, turns: [] }))).toBeNull();
  });
  it('rebuilds a finished exchange from saved turns when traces are gone', () => {
    const flow = agentFlowFor({ ...session(script.length), events: [] })!;
    expect(flow.recorded).toBe(true);
    expect(flow.status).toBe('done');
    expect(flow.steps.every((step) => step.state === 'done')).toBe(true);
  });
  it('keys lead-to-peer timing by exchange for the transcript', () => {
    expect(handoffsFor(session(script.length)).get('x1')).toEqual({
      latencyMs: 615.6,
      taskId: PEER_TASK,
    });
  });
});
