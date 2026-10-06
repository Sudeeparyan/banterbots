export type Team = {
  id: string;
  name: string;
  abbreviation: string;
  color: string;
  alternate_color: string;
  logo: string;
};
export type Game = {
  id: string;
  home_team: string;
  away_team: string;
  start_time: string;
  mode: 'replay' | 'live';
  status: string;
  label: string;
  home_score: number;
  away_score: number;
  quarter: number;
  clock: string;
  play_count?: number;
};
export type PlayEvent = {
  source?: string;
  id: string;
  sequence: number;
  revision: number;
  quarter: number;
  clock: string;
  home_team: string;
  away_team: string;
  home_score: number;
  away_score: number;
  possession: string | null;
  offense_team?: string | null;
  scoring_team?: string | null;
  down: number | null;
  distance: number | null;
  yardline: number | null;
  description: string;
  play_type: string;
  flags: string[];
  drive: number | null;
  yards_gained: number | null;
  observed_at: string;
  occurred_at: string | null;
};
export type Snapshot = {
  id: string;
  hash: string;
  event: PlayEvent;
  drive_history: Record<string, unknown>[];
  recent_turns: Record<string, unknown>[];
};
export type Turn = {
  id: string;
  exchange_id: string;
  agent_id: 'a' | 'b';
  persona: string;
  team_id: string;
  event_id: string;
  snapshot_hash: string;
  text: string;
  spoken_text: string | null;
  kind: string;
  emotion: string;
  created_at: string;
  voice: string;
  mode: 'demo' | 'openai';
  reply_to_turn_id: string | null;
  task_id: string | null;
};
export type RunEvent = {
  seq: number;
  session_id: string;
  epoch: number;
  type: string;
  timestamp: string;
  data: Record<string, unknown>;
};
export type Feed = {
  status: string;
  last_success?: string | null;
  last_play_at?: string | null;
  latest_play_at?: string | null;
  error?: string | null;
};
export type Session = {
  date?: string | null;
  id: string;
  epoch: number;
  seq?: number;
  history_start_seq?: number;
  created_at?: string;
  game: Game;
  mode: 'live' | 'replay';
  provider: 'demo' | 'openai';
  status: string;
  speed: number;
  index: number;
  total: number;
  snapshot?: Snapshot | null;
  turns: Turn[];
  events: RunEvent[];
  feed: Feed;
  metrics: Record<string, number | null>;
};
export type Config = {
  openai_configured: boolean;
  text_model: string;
  voice_model: string;
  a2a_version: string;
  personas: { agent_id: 'a' | 'b'; name: string; style: string; voice: string }[];
};
export type SavedRun = {
  id: string;
  game_id?: string;
  game?: Game;
  status: string;
  mode: string;
  provider: string;
  created_at?: string;
  turn_count?: number;
};
export async function api<T>(path: string, init?: RequestInit, timeoutMs = 45000): Promise<T> {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0)
    throw new Error('Request timeout must be finite and positive');
  const headers = new Headers(init?.headers);
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const callerSignal = init?.signal;
  if (callerSignal?.aborted)
    throw callerSignal.reason ?? new DOMException('Request cancelled', 'AbortError');
  const controller = new AbortController();
  const cancel = () => controller.abort(callerSignal?.reason);
  callerSignal?.addEventListener('abort', cancel, { once: true });
  const timeoutError = new Error(
    'The request timed out. Check the studio connection and try again.',
  );
  timeoutError.name = 'TimeoutError';
  const timer = setTimeout(() => controller.abort(timeoutError), timeoutMs);
  let aborted: () => void = () => {};
  const cancellation = new Promise<never>((_, reject) => {
    aborted = () =>
      reject(controller.signal.reason ?? new DOMException('Request cancelled', 'AbortError'));
    controller.signal.addEventListener('abort', aborted, { once: true });
  });
  const request = async (): Promise<T> => {
    const response = await fetch('/api' + path, { ...init, headers, signal: controller.signal });
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = await response.json();
        detail =
          typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? body);
      } catch {
        /* response can be plain text */
      }
      throw new Error(detail || `Studio request failed (${response.status})`);
    }
    return response.json() as Promise<T>;
  };
  try {
    // Bound the complete response, including its JSON body. Racing cancellation
    // also keeps the UI recoverable if a browser fetch implementation stalls.
    return await Promise.race([request(), cancellation]);
  } finally {
    clearTimeout(timer);
    callerSignal?.removeEventListener('abort', cancel);
    controller.signal.removeEventListener('abort', aborted);
  }
}
export const post = <T>(path: string, body: unknown) =>
  api<T>(path, { method: 'POST', body: JSON.stringify(body) });
