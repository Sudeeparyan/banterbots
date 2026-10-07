import { useCallback, useEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import {
  Activity,
  ArrowDown,
  ArrowRight,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Code2,
  Download,
  ExternalLink,
  History,
  Layers,
  LoaderCircle,
  MessageCircle,
  Mic,
  Pause,
  Play,
  Radio,
  RotateCcw,
  Settings2,
  ShieldCheck,
  SkipForward,
  Sparkles,
  Terminal,
  Volume2,
  VolumeX,
  Wifi,
  WifiOff,
  X,
  Zap,
} from 'lucide-react';
import { api, post } from './types';
import type { Config, Feed, Game, RunEvent, SavedRun, Session, Team, Turn } from './types';
import { mergeSessionView, reduceEvent } from './session';
import { BroadcastAudio } from './audio';
import { exchangesFor, nflDate, transcriptFor } from './broadcast';

const asText = (value: unknown) =>
  typeof value === 'string' ? value : JSON.stringify(value, null, 2);
const duration = (value: number | null | undefined) =>
  value == null ? '—' : `${Math.round(value)} ms`;
const time = (value?: string | null) =>
  value
    ? new Date(value).toLocaleTimeString('en-GB', {
        timeZone: 'Europe/Dublin',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      })
    : '—';
const ordinal = (value: number | null | undefined) =>
  value == null
    ? '—'
    : `${value}${value === 1 ? 'st' : value === 2 ? 'nd' : value === 3 ? 'rd' : 'th'}`;
const gameDate = (value: string) =>
  new Date(value).toLocaleDateString('en-GB', {
    timeZone: 'America/New_York',
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  });
const teamColor = (team?: Team): CSSProperties =>
  ({ '--team': team?.color ?? '#b7f344' }) as CSSProperties;
function Logo({ team, size = 38 }: { team?: Team; size?: number }) {
  return team ? (
    <img
      className="team-logo"
      width={size}
      height={size}
      src={team.logo}
      alt={`${team.name} logo`}
      onError={(e) => {
        e.currentTarget.style.visibility = 'hidden';
      }}
    />
  ) : (
    <span className="team-placeholder" style={{ width: size, height: size }}>
      NFL
    </span>
  );
}
function Wave({ active = false }: { active?: boolean }) {
  return (
    <div className={`wave ${active ? 'active' : ''}`} aria-hidden="true">
      {Array.from({ length: 18 }, (_, i) => (
        <i
          key={i}
          style={{
            height: `${[12, 21, 9, 28, 17, 34, 24, 13, 30, 18, 26, 36, 15, 27, 11, 23, 18, 8][i]}px`,
            animationDelay: `${i * 0.075}s`,
          }}
        />
      ))}
    </div>
  );
}
function App() {
  type Highlight = {
    index: number;
    id: string;
    label: string;
    quarter: number;
    clock: string;
    category: string;
  };
  const [teams, setTeams] = useState<Team[]>([]),
    [games, setGames] = useState<Game[]>([]),
    [config, setConfig] = useState<Config | null>(null);
  const [session, setSession] = useState<Session | null>(null),
    [saved, setSaved] = useState<SavedRun[]>([]);
  const [mode, setMode] = useState<'replay' | 'live'>('replay'),
    [provider, setProvider] = useState<'demo' | 'openai'>('demo');
  const [selectedTeam, setSelectedTeam] = useState<string | null>(null),
    [selectedGame, setSelectedGame] = useState('2024_01_BAL_KC');
  const [date, setDate] = useState(nflDate()),
    [feed, setFeed] = useState<Feed>({ status: 'connecting' });
  const [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  const [muted, setMuted] = useState(true),
    [speaking, setSpeaking] = useState<string | null>(null),
    [connected, setConnected] = useState(false);
  const [debug, setDebug] = useState(false),
    [debugTab, setDebugTab] = useState('timeline'),
    [historyOpen, setHistoryOpen] = useState(false),
    [help, setHelp] = useState(false);
  const [traceSelected, setTraceSelected] = useState<number | null>(null),
    [readingSaved, setReadingSaved] = useState(false);
  const [captions, setCaptions] = useState<
    Record<string, { agent_id: string; persona: string; text: string }>
  >({});
  const [highlights, setHighlights] = useState<Highlight[]>([]),
    [startingHighlight, setStartingHighlight] = useState(false),
    [now, setNow] = useState(Date.now());
  const [following, setFollowing] = useState(true),
    [health, setHealth] = useState<{ a: boolean; b: boolean } | null>(null),
    [connectionAttempt, setConnectionAttempt] = useState(0);
  const followingRef = useRef(true);
  const conversationRef = useRef<HTMLDivElement | null>(null);
  const teamRail = useRef<HTMLDivElement | null>(null);
  const feedRequest = useRef(0);
  const controlPending = useRef(false);
  const openPending = useRef(false);
  const sessionRequest = useRef(0);
  const socket = useRef<WebSocket | null>(null),
    seq = useRef(0),
    epoch = useRef(0),
    sessionRef = useRef<Session | null>(null),
    mutedRef = useRef(true),
    savedRef = useRef(false);
  const transcriptEnd = useRef<HTMLDivElement | null>(null),
    audioRef = useRef<BroadcastAudio | null>(null);
  if (!audioRef.current)
    audioRef.current = new BroadcastAudio(
      setSpeaking,
      (id, samples, cancelled) => {
        if (socket.current?.readyState === WebSocket.OPEN)
          socket.current.send(
            JSON.stringify({
              type: 'playback.ack',
              segment_id: id,
              played_samples: samples,
              cancelled,
            }),
          );
      },
      (message, audioMuted) => {
        setError(message);
        if (audioMuted) {
          mutedRef.current = true;
          setMuted(true);
        }
      },
    );
  const findTeam = useCallback(
    (id?: string | null) => teams.find((t) => t.id === id || t.abbreviation === id),
    [teams],
  );
  useEffect(() => {
    sessionRef.current = session;
  }, [session]);
  useEffect(() => {
    mutedRef.current = muted;
  }, [muted]);
  useEffect(() => {
    savedRef.current = readingSaved;
  }, [readingSaved]);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    let alive = true;
    let pending = false;
    const poll = async () => {
      if (pending) return;
      pending = true;
      try {
        const result = await api<{ agents: { a: boolean; b: boolean } }>('/health');
        if (alive) setHealth(result.agents);
      } catch {
        if (alive) setHealth({ a: false, b: false });
      } finally {
        pending = false;
      }
    };
    void poll();
    const timer = setInterval(() => void poll(), 10000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [connectionAttempt]);
  useEffect(() => {
    if (mode !== 'replay') {
      setHighlights([]);
      return;
    }
    let alive = true;
    setHighlights([]);
    void api<{ highlights: Highlight[] }>(`/games/${selectedGame}/highlights`)
      .then((data) => {
        if (alive) setHighlights(data.highlights);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [selectedGame, mode]);
  useEffect(() => {
    if (!historyOpen && !help) return;
    const previous = document.activeElement as HTMLElement | null;
    const modal = document.querySelector<HTMLElement>('[role="dialog"]');
    modal?.querySelector<HTMLElement>('button')?.focus();
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setHistoryOpen(false);
        setHelp(false);
      }
      if (event.key !== 'Tab' || !modal) return;
      const targets = Array.from(
        modal.querySelectorAll<HTMLElement>(
          'button:not(:disabled),a[href],input,select,[tabindex="0"]',
        ),
      );
      const first = targets[0],
        last = targets[targets.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    document.addEventListener('keydown', key);
    return () => {
      document.removeEventListener('keydown', key);
      previous?.focus();
    };
  }, [historyOpen, help]);

  const loadGames = useCallback(async (currentMode: 'replay' | 'live', currentDate: string) => {
    const request = ++feedRequest.current;
    try {
      const result = await api<{ games: Game[]; feed: Feed }>(
        `/games?mode=${currentMode}&date=${currentDate.replaceAll('-', '')}`,
      );
      if (request === feedRequest.current) {
        setGames(result.games);
        setFeed(result.feed);
        if (result.feed.error) setError(`Live feed unavailable. ${result.feed.error}`);
        else
          setError((previous) => (previous.startsWith('Live feed unavailable.') ? '' : previous));
      }
      return result.games;
    } catch (e) {
      if (request === feedRequest.current)
        setFeed((previous) => ({ ...previous, status: 'error', error: (e as Error).message }));
      throw e;
    }
  }, []);
  const loadSaved = useCallback(async () => {
    try {
      setSaved(await api<SavedRun[]>('/sessions'));
    } catch {
      /* saved list is optional to primary broadcast */
    }
  }, []);
  const openSession = useCallback(
    async (
      gameId: string,
      currentMode: 'replay' | 'live',
      currentProvider: 'demo' | 'openai',
      startIndex?: number,
      currentDate = date,
    ) => {
      if (openPending.current) return null;
      openPending.current = true;
      const request = ++sessionRequest.current;
      const wasSaved = savedRef.current;
      setBusy(true);
      setError('');
      audioRef.current?.clear();
      setCaptions({});
      try {
        const bookmarks =
          currentMode === 'replay' && startIndex === undefined
            ? await api<{ highlights: Highlight[] }>(`/games/${gameId}/highlights`).catch(() => ({
                highlights: [],
              }))
            : null;
        const position =
          startIndex ??
          bookmarks?.highlights.find((item) => item.category === 'opening_drive')?.index ??
          0;
        if (sessionRef.current && !wasSaved)
          await post(`/sessions/${sessionRef.current.id}/control`, { action: 'stop' }).catch(
            () => {},
          );
        const next = await post<Session>('/sessions', {
          game_id: gameId,
          mode: currentMode,
          provider: currentProvider,
          voice_enabled: true,
          speed: sessionRef.current?.speed ?? 2,
          start_index: position,
          ...(currentMode === 'live' ? { date: currentDate.replaceAll('-', '') } : {}),
        });
        if (request !== sessionRequest.current) return null;
        setReadingSaved(false);
        savedRef.current = false;
        if (socket.current) {
          socket.current.onmessage = null;
          socket.current.onclose = null;
          socket.current.close();
          socket.current = null;
        }
        setConnected(false);
        seq.current = Math.max(next.seq ?? 0, ...(next.events ?? []).map((e) => e.seq));
        epoch.current = next.epoch;
        setSession(next);
        sessionRef.current = next;
        setSelectedGame(gameId);
        followingRef.current = true;
        setFollowing(true);
        void loadSaved();
        return next;
      } catch (e) {
        setError((e as Error).message);
        return null;
      } finally {
        openPending.current = false;
        setBusy(false);
      }
    },
    [loadSaved, date],
  );
  useEffect(() => {
    let alive = true;
    setLoading(true);
    void (async () => {
      try {
        const [teamList, configuration, gameList] = await Promise.all([
          api<Team[]>('/teams'),
          api<Config>('/config'),
          loadGames('replay', date),
        ]);
        if (!alive) return;
        setTeams(teamList);
        setConfig(configuration);
        await loadSaved();
        if (!alive) return;
        if (gameList.length)
          await openSession(
            gameList.find((g) => g.id === '2024_01_BAL_KC')?.id ?? gameList[0].id,
            'replay',
            'demo',
          );
      } catch (e) {
        if (alive) setError(`Could not connect to the studio. ${(e as Error).message}`);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [connectionAttempt]); // Explicit retry also recovers an initially unavailable API.

  useEffect(() => {
    if (!session?.id || readingSaved) {
      setConnected(false);
      return;
    }
    let disposed = false,
      retry: ReturnType<typeof setTimeout>;
    let ownedSocket: WebSocket | null = null;
    const id = session.id;
    const connect = () => {
      if (disposed) return;
      const ws = new WebSocket(
        `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/sessions/${id}/stream?since=${seq.current}`,
      );
      socket.current = ws;
      ownedSocket = ws;
      ws.onopen = () => {
        if (disposed || socket.current !== ws) return;
        setConnected(true);
      };
      ws.onmessage = (event) => {
        if (disposed || socket.current !== ws || sessionRef.current?.id !== id) return;
        let item: RunEvent;
        try {
          item = JSON.parse(event.data);
        } catch {
          return;
        }
        if (item.session_id !== id || item.seq <= seq.current || item.epoch < epoch.current) return;
        const historical = Boolean(
          item.data.historical || (item as RunEvent & { historical?: boolean }).historical,
        );
        seq.current = item.seq;
        if (item.epoch > epoch.current) {
          epoch.current = item.epoch;
          audioRef.current?.clear();
          setCaptions({});
        }
        setSession((current) => (current ? reduceEvent(current, item) : current));
        if (item.type === 'turn') {
          const turn = item.data.turn as Turn;
          if (turn) {
            setCaptions((previous) => {
              const next = { ...previous };
              delete next[turn.id];
              return next;
            });
          }
          if (turn?.mode === 'demo' && !historical)
            audioRef.current?.demo(turn.spoken_text ?? turn.text, turn.agent_id, turn.id);
        }
        if (item.type === 'turn.start' && !historical)
          setCaptions((previous) => ({
            ...previous,
            [String(item.data.turn_id)]: {
              agent_id: String(item.data.agent_id),
              persona: String(
                item.data.persona ?? (item.data.agent_id === 'a' ? 'Max Carter' : 'Riley Brooks'),
              ),
              text: '',
            },
          }));
        if (item.type === 'caption.delta' && !historical)
          setCaptions((previous) => {
            const id = String(item.data.turn_id),
              old = previous[id];
            return {
              ...previous,
              [id]: {
                agent_id: String(item.data.agent_id ?? old?.agent_id ?? 'a'),
                persona:
                  old?.persona ?? (item.data.agent_id === 'b' ? 'Riley Brooks' : 'Max Carter'),
                text: String(item.data.text ?? `${old?.text ?? ''}${item.data.delta ?? ''}`),
              },
            };
          });
        if (item.type === 'audio' && !historical)
          audioRef.current?.chunk(item.data as unknown as Parameters<BroadcastAudio['chunk']>[0]);
        if (item.type === 'segment.sealed' && !item.data.browser_speech && !historical)
          audioRef.current?.seal(item.data as unknown as Parameters<BroadcastAudio['seal']>[0]);
        if (item.type === 'error' && !historical)
          setError(asText(item.data.message ?? item.data.error ?? item.data));
      };
      ws.onclose = () => {
        if (disposed || socket.current !== ws) return;
        setConnected(false);
        audioRef.current?.clear();
        setCaptions({});
        if (!disposed) retry = setTimeout(connect, 1200);
      };
      ws.onerror = () => {
        if (!disposed && socket.current === ws) ws.close();
      };
    };
    connect();
    return () => {
      disposed = true;
      clearTimeout(retry);
      if (socket.current === ownedSocket) {
        audioRef.current?.clear();
        socket.current = null;
      }
      if (ownedSocket) {
        ownedSocket.onopen = null;
        ownedSocket.onmessage = null;
        ownedSocket.onclose = null;
        ownedSocket.onerror = null;
        ownedSocket.close();
      }
    };
  }, [session?.id, readingSaved]);

  useEffect(() => {
    if (mode !== 'live' || readingSaved) return;
    const poll = setInterval(() => {
      void loadGames(mode, date).catch(() => {});
    }, 15000);
    return () => clearInterval(poll);
  }, [mode, date, loadGames, readingSaved]);
  useEffect(() => {
    const viewport = conversationRef.current;
    if (viewport && followingRef.current)
      viewport.scrollTo({ top: viewport.scrollHeight, behavior: 'instant' });
  }, [
    session?.turns.length,
    debug,
    Object.values(captions)
      .map((c) => c.text)
      .join(''),
  ]);
  const control = async (action: string, speed?: number) => {
    if (!session || busy || controlPending.current || readingSaved || startingHighlight) return;
    if (['play', 'step'].includes(action) && (!connected || (health && (!health.a || !health.b)))) {
      setError('The studio and both commentators must be connected before starting.');
      return;
    }
    controlPending.current = true;
    setBusy(true);
    setError('');
    if (['pause', 'restart', 'stop'].includes(action)) audioRef.current?.clear();
    try {
      if (['play', 'step'].includes(action) && !muted) await audioRef.current?.unlock();
      const next = await post<Session>(`/sessions/${session.id}/control`, {
        action,
        ...(speed ? { speed } : {}),
      });
      if (sessionRef.current?.id !== next.id) return;
      if (next.epoch > epoch.current) {
        epoch.current = next.epoch;
        audioRef.current?.clear();
        setCaptions({});
      }
      setSession((current) => mergeSessionView(current, next));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      controlPending.current = false;
      setBusy(false);
    }
  };
  const changeMode = async (next: 'replay' | 'live') => {
    if (busy || startingHighlight || loading || openPending.current) return;
    if (next === mode && !readingSaved) return;
    feedRequest.current++;
    setBusy(true);
    setError('');
    audioRef.current?.clear();
    setCaptions({});
    try {
      if (session && !readingSaved)
        await post(`/sessions/${session.id}/control`, { action: 'stop' });
      setMode(next);
      setReadingSaved(false);
      savedRef.current = false;
      setSelectedTeam(null);
      setSession(null);
      sessionRef.current = null;
      setGames([]);
      setFeed({ status: 'connecting' });
      const list = await loadGames(next, date);
      if (list.length) await openSession(list[0].id, next, provider);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const changeDate = async (next: string) => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(next)) return;
    if (busy || startingHighlight || openPending.current) return;
    feedRequest.current++;
    setBusy(true);
    setError('');
    audioRef.current?.clear();
    setCaptions({});
    try {
      if (session && !readingSaved)
        await post(`/sessions/${session.id}/control`, { action: 'stop' });
      setDate(next);
      setReadingSaved(false);
      savedRef.current = false;
      setSession(null);
      sessionRef.current = null;
      setGames([]);
      setFeed({ status: 'connecting' });
      const list = await loadGames('live', next);
      if (list.length) await openSession(list[0].id, 'live', provider, 0, next);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const changeProvider = async (next: 'demo' | 'openai') => {
    if (busy || startingHighlight || openPending.current) return;
    if (session) {
      const opened = await openSession(selectedGame, mode, next);
      if (opened) setProvider(next);
    } else setProvider(next);
  };
  const toggleVoice = async () => {
    const next = !muted;
    setMuted(next);
    audioRef.current?.mute(next);
    if (!next) {
      try {
        await audioRef.current?.unlock();
      } catch {
        setMuted(true);
        audioRef.current?.mute(true);
        setError('Your browser could not enable audio. Commentary captions are still available.');
      }
    }
  };
  const viewSaved = async (id: string) => {
    if (busy || startingHighlight || openPending.current) return;
    const request = ++sessionRequest.current;
    setBusy(true);
    setError('');
    audioRef.current?.clear();
    try {
      if (session && !readingSaved)
        await post(`/sessions/${session.id}/control`, { action: 'pause' });
      const next = await api<Session>(`/sessions/${id}`);
      if (request !== sessionRequest.current) return;
      feedRequest.current++;
      setSession(next);
      sessionRef.current = next;
      setCaptions({});
      setReadingSaved(true);
      setHistoryOpen(false);
      setSelectedGame(next.game.id);
      setMode(next.mode);
      setProvider(next.provider);
      setGames([next.game]);
      setFeed(next.feed);
      setSelectedTeam(null);
      followingRef.current = true;
      setFollowing(true);
      if (next.date)
        setDate(`${next.date.slice(0, 4)}-${next.date.slice(4, 6)}-${next.date.slice(6, 8)}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const startHighlight = async (item: Highlight) => {
    if (busy || startingHighlight) return;
    setStartingHighlight(true);
    try {
      const next = await openSession(selectedGame, 'replay', provider, item.index);
      if (next) {
        if (!muted) await audioRef.current?.unlock();
        const result = await post<Session>(`/sessions/${next.id}/control`, { action: 'step' });
        if (sessionRef.current?.id !== result.id) return;
        setSession((current) => mergeSessionView(current, result));
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setStartingHighlight(false);
    }
  };
  const followLatest = () => {
    followingRef.current = true;
    setFollowing(true);
    const viewport = conversationRef.current;
    viewport?.scrollTo({ top: viewport.scrollHeight, behavior: 'instant' });
  };
  const downloadTranscript = () => {
    if (!session) return;
    const url = URL.createObjectURL(
      new Blob([transcriptFor(session)], { type: 'text/plain;charset=utf-8' }),
    );
    const link = document.createElement('a');
    link.href = url;
    link.download = `banterbots-${session.game.away_team}-${session.game.home_team}-${session.id.slice(0, 8)}.txt`;
    link.click();
    URL.revokeObjectURL(url);
  };

  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (
        help ||
        historyOpen ||
        e.altKey ||
        e.ctrlKey ||
        e.metaKey ||
        target.isContentEditable ||
        target.closest('input,select,textarea,button,a')
      )
        return;
      if (e.code === 'Space') {
        e.preventDefault();
        void control(session?.status === 'playing' ? 'pause' : 'play');
      }
      if (
        e.code === 'ArrowRight' &&
        session?.status !== 'playing' &&
        session?.status !== 'stepping'
      ) {
        e.preventDefault();
        void control('step');
      }
      if (e.key.toLowerCase() === 'm') void toggleVoice();
      if (e.key.toLowerCase() === 'd') setDebug((current) => !current);
    };
    document.addEventListener('keydown', key);
    return () => document.removeEventListener('keydown', key);
  });

  const game = session?.game ?? games.find((g) => g.id === selectedGame),
    snapshot = session?.snapshot,
    event = snapshot?.event;
  const home = findTeam(game?.home_team),
    away = findTeam(game?.away_team),
    playing = session?.status === 'playing' || session?.status === 'running',
    stepping = session?.status === 'stepping';
  const filteredGames = games.filter(
    (g) => !selectedTeam || [g.home_team, g.away_team].includes(selectedTeam),
  );
  const selectorGames =
    game && !filteredGames.some((g) => g.id === game.id) ? [game, ...filteredGames] : filteredGames;
  const playEvents = (session?.events ?? [])
    .filter((e) => e.type === 'snapshot' && e.seq >= (session?.history_start_seq ?? 0))
    .map((e) => ({ seq: e.seq, event: (e.data.snapshot as typeof snapshot)?.event }))
    .filter((item) => item.event)
    .slice(-12)
    .reverse();
  const turns = session?.turns ?? [],
    traceEvents = (session?.events ?? []).filter((e) => !['audio'].includes(e.type));
  const exchanges = exchangesFor(session);
  const agentUnavailable = health && (!health.a || !health.b);
  const selectedTrace =
    traceEvents.find((e) => e.seq === traceSelected) ?? traceEvents[traceEvents.length - 1];
  const sourceFeed = session?.feed ?? feed,
    activeFeed = {
      ...sourceFeed,
      last_play_at: sourceFeed.latest_play_at ?? sourceFeed.last_play_at,
    },
    lastA = [...turns].reverse().find((t) => t.agent_id === 'a'),
    lastB = [...turns].reverse().find((t) => t.agent_id === 'b');
  const progress = session?.total ? Math.min(100, (session.index / session.total) * 100) : 0;
  const elapsed = (value?: string | null) => {
    const delta = value ? Math.max(0, Math.floor((now - Date.parse(value)) / 1000)) : null;
    return delta === null || !Number.isFinite(delta)
      ? 'Waiting'
      : delta < 60
        ? `${delta}s ago`
        : `${Math.floor(delta / 60)}m ${delta % 60}s ago`;
  };
  const stageStart = traceEvents
    .map((e) => e.type === 'trace' && e.data.node === 'normalize')
    .lastIndexOf(true);
  const stageEvents = stageStart < 0 ? [] : traceEvents.slice(stageStart);
  const leadCaption = Object.values(captions).find((c) => c.text || stepping || playing);
  const currentLead =
    leadCaption?.persona ??
    ([...traceEvents].reverse().find((e) => e.type === 'trace' && e.data.node === 'choose_lead')
      ?.data.lead === 'b'
      ? 'Riley Brooks'
      : 'Max Carter');
  const visibleError =
    error ||
    (mode === 'live' && !readingSaved && activeFeed.error ? `Game feed: ${activeFeed.error}` : '');
  const lastTurn = turns[turns.length - 1];
  return (
    <div className="app-shell">
      <header className="site-header">
        <a className="brand" href="/" aria-label="BanterBots home">
          <span className="brand-mark">
            b<span>.</span>
          </span>
          <span>
            banter<span className="brand-light">bots</span>
            <sup>STUDIO</sup>
          </span>
        </a>
        <nav className="main-nav">
          <span className="nav-active">Broadcast studio</span>
          <button
            onClick={() => {
              setHistoryOpen(true);
              void loadSaved();
            }}
          >
            <History size={15} /> Saved runs
          </button>
        </nav>
        <div className="header-right">
          <span className="connection">
            <i className={connected ? 'online' : ''} />
            {connected
              ? 'Studio connected'
              : readingSaved
                ? 'Recorded session'
                : loading
                  ? 'Connecting'
                  : session
                    ? 'Reconnecting'
                    : 'No game selected'}
          </span>
          <button
            className="icon-button"
            title="About this studio"
            aria-label="About this studio"
            onClick={() => setHelp(true)}
          >
            <Settings2 size={18} />
          </button>
          <div className="user-icon">BB</div>
        </div>
      </header>
      <section className="team-ribbon" aria-label="Filter games by NFL team">
        <button
          className={`all-teams ${!selectedTeam ? 'selected' : ''}`}
          onClick={() => setSelectedTeam(null)}
        >
          ALL
          <br />
          <strong>32</strong>
        </button>
        <button
          className="rail-arrow"
          aria-label="Previous NFL teams"
          onClick={() => teamRail.current?.scrollBy({ left: -360, behavior: 'smooth' })}
        >
          <ChevronLeft size={16} />
        </button>
        <div className="team-list" ref={teamRail}>
          {teams.map((team) => (
            <button
              key={team.id}
              className={`team-option ${selectedTeam === team.id ? 'selected' : ''}`}
              title={team.name}
              aria-label={`Filter ${team.name}`}
              aria-pressed={selectedTeam === team.id}
              onClick={() => setSelectedTeam(selectedTeam === team.id ? null : team.id)}
            >
              <Logo team={team} size={31} />
              <span>{team.abbreviation}</span>
            </button>
          ))}
          {!teams.length &&
            Array.from({ length: 20 }, (_, i) => <div className="logo-skeleton" key={i} />)}
        </div>
        <button
          className="rail-arrow"
          aria-label="More NFL teams"
          onClick={() => teamRail.current?.scrollBy({ left: 360, behavior: 'smooth' })}
        >
          <ChevronRight size={16} />
        </button>
        <span className="league-label">
          NFL <ChevronDown size={13} />
        </span>
      </section>
      <section className="score-ribbon" aria-label="Game ticker">
        <div className="ticker-label">
          <Radio size={15} />
          <span>{mode === 'replay' ? 'THE REPLAY ROOM' : 'AROUND THE LEAGUE'}</span>
        </div>
        <div className="score-list">
          {filteredGames.map((item) => (
            <button
              key={item.id}
              className={`mini-game ${selectedGame === item.id ? 'selected' : ''}`}
              onClick={() => void openSession(item.id, mode, provider)}
              disabled={busy || startingHighlight || readingSaved}
            >
              <span className="mini-teams">
                <span>
                  <Logo team={findTeam(item.away_team)} size={21} />
                  <b>{item.away_team}</b>
                  <strong>
                    {mode === 'replay' && session?.game.id === item.id
                      ? (event?.away_score ?? item.away_score)
                      : item.away_score}
                  </strong>
                </span>
                <span>
                  <Logo team={findTeam(item.home_team)} size={21} />
                  <b>{item.home_team}</b>
                  <strong>
                    {mode === 'replay' && session?.game.id === item.id
                      ? (event?.home_score ?? item.home_score)
                      : item.home_score}
                  </strong>
                </span>
              </span>
              <span className="mini-status">
                {item.mode === 'replay' ? gameDate(item.start_time) : item.status}
                <ChevronRight size={12} />
              </span>
            </button>
          ))}
          {!filteredGames.length && (
            <span className="ticker-empty">
              {selectedTeam
                ? 'No games for this team in this feed.'
                : 'No live games found for this date. Switch to Replay to enter the studio.'}
            </span>
          )}
        </div>
        <div className="ticker-source">
          <Layers size={14} />
          {mode === 'replay' ? 'nflverse data' : 'ESPN feed'}
        </div>
      </section>
      <main className="main-content">
        <div className="page-title">
          <div>
            <div className="eyebrow">
              <span /> TWO SIDES. ONE GAME.
            </div>
            <h1>
              The broadcast <em>booth.</em>
            </h1>
            <p>Real football. Rival loyalties. A conversation on every play.</p>
          </div>
          <div className="page-actions">
            <div className="mode-switch" aria-label="Feed mode">
              <button
                className={mode === 'replay' ? 'active' : ''}
                onClick={() => void changeMode('replay')}
                disabled={busy || startingHighlight || loading}
              >
                <History size={14} /> Replay
              </button>
              <button
                className={mode === 'live' ? 'active' : ''}
                onClick={() => void changeMode('live')}
                disabled={busy || startingHighlight || loading}
              >
                <Radio size={14} /> Live feed
              </button>
            </div>
            <button
              className={`debug-button ${debug ? 'active' : ''}`}
              onClick={() => setDebug(!debug)}
            >
              <Code2 size={16} /> Agent debugger<span>{debug ? 'ON' : 'OFF'}</span>
            </button>
          </div>
        </div>
        {visibleError && (
          <div className="error-banner" role="alert">
            <WifiOff size={17} />
            <span>{visibleError}</span>
            {error && (
              <button aria-label="Dismiss error" onClick={() => setError('')}>
                <X size={17} />
              </button>
            )}
          </div>
        )}
        {loading && (
          <div className="loading-banner">
            <LoaderCircle size={16} className="spin" /> Preparing the studio and loading real NFL
            play-by-play…
          </div>
        )}
        {!loading && !session && mode === 'replay' && (
          <div className="recovery-banner">
            <WifiOff size={17} /> The studio is unavailable.
            <button disabled={busy} onClick={() => setConnectionAttempt((value) => value + 1)}>
              Retry connection
            </button>
          </div>
        )}
        {agentUnavailable && !readingSaved && (
          <div className="recovery-banner" role="status">
            <Activity size={16} />
            {!health.a && !health.b
              ? 'Both commentators are offline.'
              : `${!health.a ? 'Max' : 'Riley'} is offline.`}
            <span>
              Start all three Python services with tools/dev.py. The studio checks again
              automatically.
            </span>
          </div>
        )}
        {readingSaved && (
          <div className="saved-banner">
            <History size={16} />
            <span>Recorded run • Playback is silent. This view shows the original results.</span>
            <button
              disabled={busy || startingHighlight}
              onClick={() => void openSession(selectedGame, mode, provider)}
            >
              Regenerate as a new run <ArrowRight size={14} />
            </button>
          </div>
        )}
        <div className="broadcast-note">
          <span className="mode-label">
            <i />
            {mode === 'replay' ? 'HISTORICAL REPLAY' : 'LIVE ESPN FEED'}
          </span>
          <span>
            {provider === 'demo'
              ? 'Demo commentary · real plays, scripted voices. Add an API key for generated commentary.'
              : 'OpenAI commentary · two original commentators, one shared game snapshot.'}
          </span>
          <button onClick={() => setHelp(true)}>
            How it works <ChevronRight size={14} />
          </button>
        </div>
        {mode === 'replay' && highlights.length > 0 && !readingSaved && (
          <section className="moment-strip" aria-label="Replay moments">
            <div className="moment-title">
              <Sparkles size={17} />
              <span>
                INSTANT REPLAY<small>One click. One real play. Two reactions.</small>
              </span>
            </div>
            <div className="moment-list">
              {highlights.map((item) => (
                <button
                  key={item.id}
                  disabled={busy || startingHighlight || Boolean(agentUnavailable)}
                  className={snapshot?.event.id === item.id ? 'selected' : ''}
                  onClick={() => void startHighlight(item)}
                >
                  <span className={`moment-category ${item.category}`}>{item.label}</span>
                  <span>
                    Q{item.quarter} · {item.clock}
                    <ChevronRight size={12} />
                  </span>
                </button>
              ))}
            </div>
          </section>
        )}
        <div className="studio-grid">
          <div className="game-column">
            <section className="panel game-panel">
              <div className="panel-header">
                <span>
                  <i className={`status-dot ${playing ? 'live' : ''}`} />
                  {mode === 'replay' ? 'HISTORICAL REPLAY' : 'LIVE GAME'}
                </span>
                <span className="pill subtle">
                  {mode === 'replay' ? 'REAL GAME DATA' : 'POLLING · 5 SEC'}
                </span>
              </div>
              <div className="game-selector">
                <label htmlFor="game">IN THE BOOTH</label>
                <select
                  id="game"
                  value={selectedGame}
                  disabled={busy || startingHighlight || readingSaved || !selectorGames.length}
                  onChange={(e) => void openSession(e.target.value, mode, provider)}
                >
                  {selectorGames.map((g) => (
                    <option value={g.id} key={g.id}>
                      {g.away_team} at {g.home_team} · {g.label || gameDate(g.start_time)}
                    </option>
                  ))}
                  {!selectorGames.length && (
                    <option value={selectedGame}>No games available</option>
                  )}
                </select>
                {mode === 'live' && (
                  <input
                    aria-label="Live games date"
                    type="date"
                    value={date}
                    disabled={busy || startingHighlight || readingSaved}
                    onChange={(e) => void changeDate(e.target.value)}
                  />
                )}
              </div>
              <div className="scoreboard-body">
                <div className="matchup">
                  <div className="matchup-team" style={teamColor(away)}>
                    <Logo team={away} size={79} />
                    <h2>{away?.abbreviation ?? game?.away_team ?? 'AWAY'}</h2>
                    <span>
                      {away?.name?.replace(/.*(?=\s(?:Ravens|Chiefs|Eagles)$)/, '').trim() ??
                        'Away team'}
                    </span>
                    <i className="support-tag">RILEY’S SIDE</i>
                  </div>
                  <div className="matchup-score">
                    <span className="score-kicker">
                      {event ? `Q${event.quarter} · ${event.clock}` : 'READY FOR KICKOFF'}
                    </span>
                    <div>
                      <strong>
                        {event?.away_score ?? (mode === 'live' ? game?.away_score : 0) ?? 0}
                      </strong>
                      <span>:</span>
                      <strong>
                        {event?.home_score ?? (mode === 'live' ? game?.home_score : 0) ?? 0}
                      </strong>
                    </div>
                    <span className="matchup-state">
                      {session?.status === 'completed'
                        ? mode === 'live'
                          ? 'GAME FINAL'
                          : 'REPLAY COMPLETE'
                        : playing
                          ? 'ON AIR'
                          : readingSaved
                            ? 'RECORDED'
                            : 'BOOTH STANDBY'}
                    </span>
                  </div>
                  <div className="matchup-team" style={teamColor(home)}>
                    <Logo team={home} size={79} />
                    <h2>{home?.abbreviation ?? game?.home_team ?? 'HOME'}</h2>
                    <span>
                      {home?.name?.replace(/.*(?=\s(?:Ravens|Chiefs|Eagles)$)/, '').trim() ??
                        'Home team'}
                    </span>
                    <i className="support-tag">MAX’S SIDE</i>
                  </div>
                </div>
                <div className="field-context">
                  <span className="fact-timing">
                    {event
                      ? event.source === 'nflverse'
                        ? 'SITUATION AT THE SNAP'
                        : 'LATEST FIELD SITUATION'
                      : 'FIELD CONTEXT'}
                  </span>
                  <div className="game-facts">
                    <div>
                      <span>POSSESSION</span>
                      <b>
                        {event?.possession ?? '—'}
                        {event?.possession && <span className="football-dot" />}
                      </b>
                    </div>
                    <div>
                      <span>DOWN & DISTANCE</span>
                      <b>
                        {event?.down ? `${ordinal(event.down)} & ${event.distance ?? '—'}` : '—'}
                      </b>
                    </div>
                    <div>
                      <span>TO OPPONENT'S GOAL</span>
                      <b>{event?.yardline != null ? `${event.yardline} yards` : '—'}</b>
                    </div>
                  </div>
                  <div
                    className="football-field"
                    aria-label={
                      event?.yardline != null
                        ? `Ball ${event.yardline} yards from goal`
                        : 'Football field waiting for next play'
                    }
                  >
                    <div className="endzone">{game?.away_team ?? 'NFL'}</div>
                    <div className="yard-grid">
                      {Array.from({ length: 9 }, (_, i) => (
                        <span key={i}>{i < 5 ? (i + 1) * 10 : (9 - i) * 10}</span>
                      ))}
                      {event?.yardline != null && (
                        <div
                          className="ball-marker"
                          style={{
                            left: `${Math.max(2, Math.min(98, event.possession === game?.away_team ? 100 - event.yardline : event.yardline))}%`,
                          }}
                        >
                          <span /> <b>{event.possession}</b>
                        </div>
                      )}
                    </div>
                    <div className="endzone">{game?.home_team ?? 'NFL'}</div>
                  </div>
                </div>
              </div>
              <div className="current-play">
                <span className="eyebrow">{event ? 'LATEST PLAY' : 'THE STAGE IS SET'}</span>
                <p>
                  {event?.description ??
                    'Press play to relive the game, one real play at a time. Max and Riley have a few things to say.'}
                </p>
              </div>
              <div className="transport">
                <button
                  className="icon-button outlined transport-voice"
                  onClick={() => void toggleVoice()}
                  aria-label={muted ? 'Enable commentary voice' : 'Mute commentary voice'}
                  title={muted ? 'Enable commentary voice (M)' : 'Mute commentary voice (M)'}
                  aria-pressed={!muted}
                >
                  {muted ? <VolumeX size={16} /> : <Volume2 size={16} />}
                </button>
                <button
                  className={`play-button ${playing ? 'is-playing' : ''}`}
                  disabled={
                    busy ||
                    startingHighlight ||
                    !session ||
                    readingSaved ||
                    stepping ||
                    (!playing && (Boolean(agentUnavailable) || !connected)) ||
                    session.status === 'completed'
                  }
                  onClick={() => void control(playing ? 'pause' : 'play')}
                >
                  {busy || stepping ? (
                    <LoaderCircle size={16} className="spin" />
                  ) : playing ? (
                    <Pause size={16} fill="currentColor" />
                  ) : (
                    <Play size={16} fill="currentColor" />
                  )}
                  {playing
                    ? 'Pause broadcast'
                    : stepping
                      ? 'Calling this play…'
                      : 'Start broadcast'}
                </button>
                <button
                  className="icon-button outlined"
                  disabled={
                    busy ||
                    startingHighlight ||
                    !session ||
                    readingSaved ||
                    playing ||
                    stepping ||
                    !connected ||
                    Boolean(agentUnavailable) ||
                    session.status === 'completed'
                  }
                  onClick={() => void control('step')}
                  aria-label="Advance one play"
                  title="Advance one play"
                >
                  <SkipForward size={17} />
                </button>
                <button
                  className="icon-button outlined"
                  disabled={busy || startingHighlight || !session || readingSaved}
                  onClick={() => void control('restart')}
                  aria-label="Restart game"
                  title="Restart game"
                >
                  <RotateCcw size={16} />
                </button>
                <label className="speed-label" title="Replay speed">
                  {mode === 'replay' && (
                    <>
                      <Zap size={13} />
                      <select
                        aria-label="Replay speed"
                        value={session?.speed ?? 2}
                        disabled={busy || startingHighlight || readingSaved}
                        onChange={(e) => void control('speed', Number(e.target.value))}
                      >
                        <option value={0.5}>0.5×</option>
                        <option value={1}>1×</option>
                        <option value={2}>2×</option>
                        <option value={4}>4×</option>
                        <option value={8}>8×</option>
                      </select>
                    </>
                  )}
                </label>
              </div>
              <div className="replay-progress">
                <div>
                  <i style={{ width: `${progress}%` }} />
                </div>
                <span>
                  {mode === 'replay'
                    ? `PLAY ${Math.min(session?.index ?? 0, session?.total ?? 0)} OF ${session?.total ?? '—'}`
                    : `FEED ${activeFeed.status.toUpperCase()}`}
                </span>
                <span>
                  {mode === 'replay'
                    ? 'REAL NFL PLAY-BY-PLAY'
                    : `POLLED ${time(activeFeed.last_success)}`}
                </span>
              </div>
            </section>
            <section className="panel playfeed-panel">
              <div className="panel-header">
                <span>
                  <Activity size={14} /> PLAY-BY-PLAY
                </span>
                <span className="small-muted">One shared source</span>
              </div>
              <div className="play-feed">
                {playEvents.length ? (
                  playEvents.map(({ seq: playSeq, event: play }, i) => (
                    <div className={`play-feed-item ${i === 0 ? 'latest' : ''}`} key={playSeq}>
                      <div className="play-stamp">
                        Q{play!.quarter}
                        <span>{play!.clock}</span>
                      </div>
                      <div>
                        <span
                          className={`play-type ${play!.flags?.some((f) => f.includes('touchdown')) ? 'scoring' : ''}`}
                        >
                          {play!.play_type.replaceAll('_', ' ')}
                          {play!.revision > 1 ? ' · CORRECTED' : ''}
                        </span>
                        <p>{play!.description}</p>
                      </div>
                    </div>
                  ))
                ) : (
                  <div className="feed-empty">
                    <Activity size={23} />
                    <p>The playbook is ready.</p>
                    <span>Game events appear here as the broadcast unfolds.</span>
                  </div>
                )}
              </div>
              <div className="feed-health">
                <span>
                  <Wifi size={12} />
                  {mode === 'replay'
                    ? 'Verified nflverse replay'
                    : `Poll ${activeFeed.status} · ${elapsed(activeFeed.last_success)}`}
                </span>
                <span>
                  {mode === 'replay'
                    ? 'No future plays shared'
                    : `Last play ${elapsed(activeFeed.last_play_at ?? event?.occurred_at)}`}
                </span>
              </div>
            </section>
          </div>
          <div className="booth-column">
            <section className="panel booth-panel">
              <div className="panel-header">
                <span>
                  <Mic size={15} /> THE COMMENTARY BOOTH
                </span>
                <button
                  className={`voice-control ${!muted ? 'enabled' : ''}`}
                  onClick={() => void toggleVoice()}
                  aria-pressed={!muted}
                >
                  {muted ? <VolumeX size={15} /> : <Volume2 size={15} />}Voice{' '}
                  {muted ? 'off' : 'on'}
                </button>
              </div>
              <div className="persona-grid">
                {(['a', 'b'] as const).map((agent, i) => {
                  const team = i === 0 ? home : away,
                    persona = config?.personas.find((p) => p.agent_id === agent),
                    last = i === 0 ? lastA : lastB,
                    active = speaking === agent;
                  return (
                    <div
                      className={`persona-card ${active ? 'speaking' : ''}`}
                      style={teamColor(team)}
                      key={agent}
                    >
                      <div className="persona-top">
                        <div className={`persona-avatar persona-${agent}`}>
                          <Logo team={team} size={43} />
                          <span>{agent === 'a' ? 'MC' : 'RB'}</span>
                        </div>
                        <span className="agent-badge">
                          AGENT {agent.toUpperCase()}
                          <i className={health?.[agent] === false ? 'agent-offline' : ''} />
                        </span>
                      </div>
                      <h3>{persona?.name ?? (i === 0 ? 'Max Carter' : 'Riley Brooks')}</h3>
                      <p>
                        {i === 0
                          ? 'Big energy. Bigger home-team bias.'
                          : 'Cool head. Sharp reads. Away-team loyal.'}
                      </p>
                      <div className="persona-team">
                        <Logo team={team} size={22} />
                        <span>
                          Backing the{' '}
                          <strong>
                            {team?.abbreviation ?? (i === 0 ? 'home' : 'away')}{' '}
                            {i === 0 ? 'home' : 'away'} team
                          </strong>
                        </span>
                      </div>
                      <div className="persona-footer">
                        <Wave active={active} />
                        <span>
                          {active
                            ? 'SPEAKING'
                            : Object.values(captions).some((c) => c.agent_id === agent)
                              ? 'PREPARING CALL'
                              : health && !health[agent]
                                ? 'OFFLINE'
                                : last
                                  ? 'CALL COMPLETE'
                                  : 'READY'}
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
              <div className="booth-flow">
                <span>
                  <ShieldCheck size={13} /> Shared play
                </span>
                <ArrowRight size={13} />
                <span>
                  <MessageCircle size={13} /> Call + reply
                </span>
                <ArrowRight size={13} />
                <span>Next play</span>
              </div>
            </section>
            <section className="panel conversation-panel">
              <div className="panel-header">
                <span>
                  <MessageCircle size={15} /> THE CALL & RESPONSE{' '}
                  <b className="count">{turns.length}</b>
                </span>
                <span className={`pill ${provider === 'demo' ? 'demo' : 'subtle'}`}>
                  {provider === 'demo' ? 'DEMO COMMENTARY' : 'GPT-LIVE AUDIO'}
                </span>
              </div>
              <div className="handoff-line">
                <span className={playing || stepping ? 'handoff-status working' : 'handoff-status'}>
                  <i />
                  {readingSaved ? 'RECORDED' : playing || stepping ? 'ON AIR' : 'STANDBY'}
                </span>
                <span>
                  {playing || stepping
                    ? `${currentLead} is in the booth`
                    : lastTurn?.reply_to_turn_id
                      ? 'Call delivered. Rival answered.'
                      : lastTurn
                        ? 'Lead call saved. Exchange interrupted.'
                        : 'Ready when you are.'}
                </span>
                <strong>{turns.filter((turn) => turn.reply_to_turn_id).length} exchanges</strong>
              </div>
              {!following && (
                <button className="follow-latest" onClick={followLatest}>
                  <ArrowDown size={13} /> Back to the latest exchange
                </button>
              )}
              <div
                className="conversation"
                ref={conversationRef}
                onScroll={(e) => {
                  const element = e.currentTarget;
                  const nearBottom =
                    element.scrollHeight - element.scrollTop - element.clientHeight < 56;
                  followingRef.current = nearBottom;
                  setFollowing(nearBottom);
                }}
                aria-live="polite"
                aria-label="Agent commentary transcript"
              >
                {turns.length
                  ? exchanges.map((exchange, i) => (
                      <section
                        className={`exchange-group ${i === exchanges.length - 1 ? 'latest-exchange' : ''}`}
                        key={exchange.id}
                        aria-label={`Exchange ${i + 1}`}
                      >
                        <div className="exchange-context">
                          <span className="exchange-number">{String(i + 1).padStart(2, '0')}</span>
                          <strong>
                            {exchange.snapshot
                              ? `Q${exchange.snapshot.event.quarter} · ${exchange.snapshot.event.clock}`
                              : `Play ${exchange.turns[0].event_id.split('_').pop()}`}
                          </strong>
                          <span className="exchange-play-type">
                            {exchange.snapshot?.event.play_type.replaceAll('_', ' ') ??
                              'Shared play'}
                          </span>
                          {exchange.snapshot && (
                            <span className="exchange-score">
                              {exchange.snapshot.event.away_team}{' '}
                              {exchange.snapshot.event.away_score} –{' '}
                              {exchange.snapshot.event.home_score}{' '}
                              {exchange.snapshot.event.home_team}
                            </span>
                          )}
                        </div>
                        {exchange.snapshot && (
                          <details className="exchange-facts">
                            <summary>
                              <Layers size={12} /> Shared play <ChevronDown size={12} />
                            </summary>
                            <p>{exchange.snapshot.event.description}</p>
                          </details>
                        )}
                        {exchange.turns.map((turn) => (
                          <article
                            className={`commentary-turn agent-${turn.agent_id} ${speaking === turn.agent_id && turn.id === (turn.agent_id === 'a' ? lastA : lastB)?.id ? 'talking' : ''}`}
                            key={turn.id}
                            style={teamColor(findTeam(turn.team_id))}
                          >
                            <div className="turn-avatar">
                              <Logo team={findTeam(turn.team_id)} size={24} />
                            </div>
                            <div className="turn-body">
                              <div className="turn-meta">
                                <strong>{turn.persona}</strong>
                                <span className="turn-team">{turn.team_id}</span>
                                <span className="turn-kind">
                                  {turn.reply_to_turn_id ? (
                                    <>
                                      <ArrowDown size={10} /> REPLY
                                    </>
                                  ) : (
                                    'CALL'
                                  )}
                                </span>
                                <time>{time(turn.created_at)}</time>
                              </div>
                              <p>
                                {turn.mode === 'openai'
                                  ? (turn.spoken_text ?? turn.text)
                                  : turn.text}
                              </p>
                              <div className={`turn-proof ${debug ? 'show-proof' : ''}`}>
                                <Check size={11} />
                                <span>
                                  Play {turn.event_id.split('_').pop()} · snapshot{' '}
                                  {turn.snapshot_hash.slice(0, 8)}
                                </span>
                              </div>
                            </div>
                          </article>
                        ))}
                      </section>
                    ))
                  : !Object.keys(captions).length && (
                      <div className="conversation-empty">
                        <div className="empty-mics">
                          <div>
                            <Mic size={27} />
                          </div>
                          <span>vs.</span>
                          <div>
                            <Mic size={27} />
                          </div>
                        </div>
                        <h3>Great rivals make great radio.</h3>
                        <p>
                          Pick an instant replay above, or call the opening drive.
                          <br />
                          Max calls it. Riley has something to say.
                        </p>
                        <button
                          className="empty-start"
                          disabled={
                            busy ||
                            !session ||
                            readingSaved ||
                            playing ||
                            stepping ||
                            !connected ||
                            Boolean(agentUnavailable)
                          }
                          onClick={() => void control('step')}
                        >
                          <Play size={15} fill="currentColor" /> Call the first play
                        </button>
                        <div>
                          <span>
                            <i /> MAX CARTER
                          </span>
                          <span>
                            <i /> RILEY BROOKS
                          </span>
                        </div>
                      </div>
                    )}
                {Object.entries(captions).map(([id, caption]) => (
                  <article className={`commentary-turn agent-${caption.agent_id} talking`} key={id}>
                    <div className="turn-avatar">{caption.agent_id === 'a' ? 'M' : 'R'}</div>
                    <div className="turn-body">
                      <div className="turn-meta">
                        <strong>{caption.persona}</strong>
                        <span className="turn-kind">ON THE MIC</span>
                      </div>
                      <p>
                        {caption.text || 'Reading the play…'}
                        <span className="caption-cursor" />
                      </p>
                    </div>
                  </article>
                ))}
                {playing && !speaking && (
                  <div className="thinking">
                    <i />
                    <i />
                    <i />
                    <span>Reading the field…</span>
                  </div>
                )}
                <div ref={transcriptEnd} />
              </div>
              <div className="conversation-footer">
                <span>
                  <i className={playing ? 'live' : ''} />
                  {readingSaved
                    ? 'Original transcript'
                    : playing
                      ? 'Agents are on air'
                      : 'The booth is waiting'}
                </span>
                <span>
                  {muted ? 'Captions only' : 'Captions + voice'} <Mic size={12} />
                </span>
                <button
                  disabled={!turns.length}
                  onClick={downloadTranscript}
                  aria-label="Download commentary transcript"
                >
                  <Download size={13} /> Transcript
                </button>
              </div>
            </section>
          </div>
        </div>
        <section className="studio-status">
          <div>
            <span className="eyebrow">ENGINE ROOM</span>
            <select
              aria-label="Commentary provider"
              value={provider}
              disabled={busy || startingHighlight || playing || readingSaved}
              onChange={(e) => void changeProvider(e.target.value as typeof provider)}
            >
              <option value="demo">Demo · no API key needed</option>
              <option value="openai" disabled={!config?.openai_configured}>
                OpenAI ·{' '}
                {config?.openai_configured ? 'key configured' : 'add credentials to enable'}
              </option>
            </select>
          </div>
          <div className="engine-metrics">
            <span>
              Text latency{' '}
              <strong>
                {duration(
                  session?.metrics.first_text_ms ?? session?.metrics.ingestion_to_first_text_ms,
                )}
              </strong>
            </span>
            <span>
              Audio latency{' '}
              <strong>
                {duration(
                  session?.metrics.first_audio_ms ?? session?.metrics.ingestion_to_first_audio_ms,
                )}
              </strong>
            </span>
            <span>
              Peer exchanges <strong>{turns.filter((t) => t.reply_to_turn_id).length}</strong>
            </span>
          </div>
        </section>
        {debug && (
          <section className="panel debugger">
            <div className="panel-header">
              <span>
                <Terminal size={16} /> AGENT DEBUGGER{' '}
                <span className="debug-version">
                  LangGraph / A2A {config?.a2a_version ?? '1.0'}
                </span>
              </span>
              <div>
                <a
                  className="export-link"
                  href={session ? `/api/sessions/${session.id}/export` : '#'}
                  download={`banterbots-${session?.id}.json`}
                >
                  <Download size={13} /> Export trace
                </a>
                <button
                  className="icon-button"
                  aria-label="Close debugger"
                  onClick={() => setDebug(false)}
                >
                  <X size={15} />
                </button>
              </div>
            </div>
            <div className="graph-flow" aria-label="Agent workflow">
              {[
                ['normalize', 'Read play'],
                ['snapshot', 'Freeze facts'],
                ['choose_lead', 'Choose lead'],
                ['a2a_exchange', 'Call + peer reply'],
                ['finalize', 'Save exchange'],
              ].map(([node, label], i) => {
                const matching = stageEvents.filter(
                    (e) => e.type === 'trace' && e.data.node === node,
                  ),
                  done = matching.some((e) => e.data.phase === 'complete'),
                  started = matching.some((e) => e.data.phase === 'start');
                return (
                  <div key={node} className={done ? 'done' : started ? 'working' : ''}>
                    <span>
                      {done ? (
                        <Check size={14} />
                      ) : started ? (
                        <LoaderCircle size={14} className="spin" />
                      ) : (
                        i + 1
                      )}
                    </span>
                    <strong>{label}</strong>
                    {i < 4 && <ChevronRight size={14} />}
                  </div>
                );
              })}
            </div>
            <div className="debug-tabs">
              {['timeline', 'shared facts', 'prompts & outputs', 'metrics'].map((tab) => (
                <button
                  key={tab}
                  className={debugTab === tab ? 'active' : ''}
                  onClick={() => setDebugTab(tab)}
                >
                  {tab}
                </button>
              ))}
            </div>
            {debugTab === 'timeline' ? (
              <div className="trace-grid">
                <div className="trace-list">
                  {traceEvents
                    .slice(-80)
                    .reverse()
                    .map((trace) => (
                      <button
                        key={trace.seq}
                        className={selectedTrace?.seq === trace.seq ? 'active' : ''}
                        onClick={() => setTraceSelected(trace.seq)}
                      >
                        <span className={`trace-kind ${trace.type === 'error' ? 'error' : ''}`}>
                          {trace.type}
                        </span>
                        <span>
                          {asText(
                            trace.data.node ?? trace.data.stage ?? trace.data.message ?? '',
                          ).slice(0, 65)}
                        </span>
                        <time>{time(trace.timestamp)}</time>
                      </button>
                    ))}
                  {!traceEvents.length && (
                    <p>No trace events yet. Advance a play to inspect the graph.</p>
                  )}
                </div>
                <pre>
                  {selectedTrace
                    ? JSON.stringify(selectedTrace, null, 2)
                    : 'The selected event will appear here.'}
                </pre>
              </div>
            ) : debugTab === 'shared facts' ? (
              <pre>
                {JSON.stringify(
                  snapshot ?? {
                    message: 'No snapshot yet. Both agents receive the exact same sealed snapshot.',
                  },
                  null,
                  2,
                )}
              </pre>
            ) : debugTab === 'prompts & outputs' ? (
              <div className="prompt-list">
                {traceEvents
                  .filter(
                    (e) =>
                      e.type.includes('prompt') ||
                      e.type === 'turn' ||
                      (e.type === 'trace' && JSON.stringify(e.data).includes('prompt')),
                  )
                  .slice(-25)
                  .map((e) => (
                    <details key={e.seq}>
                      <summary>
                        {e.type} ·{' '}
                        {asText(
                          e.data.node ??
                            e.data.agent_id ??
                            (e.data.turn as Turn)?.persona ??
                            'agent',
                        )}
                        <span>{time(e.timestamp)}</span>
                      </summary>
                      <pre>{JSON.stringify(e.data, null, 2)}</pre>
                    </details>
                  ))}
                {!traceEvents.some((e) => e.type === 'turn') && (
                  <p>Agent prompts and generated outputs appear here after a play.</p>
                )}
              </div>
            ) : (
              <pre>
                {JSON.stringify(
                  {
                    session_id: session?.id,
                    epoch: session?.epoch,
                    status: session?.status,
                    metrics: session?.metrics,
                    feed: activeFeed,
                    transport: connected ? 'WebSocket connected' : 'Disconnected',
                    text_model: config?.text_model,
                    voice_model: config?.voice_model,
                  },
                  null,
                  2,
                )}
              </pre>
            )}
          </section>
        )}
        <footer className="footer">
          <span>
            <span className="footer-brand">banterbots.</span> A rivalry worth listening to.
          </span>
          <span>
            Historical data:{' '}
            <a href="https://github.com/nflverse/nflverse-data" target="_blank" rel="noreferrer">
              nflverse <ExternalLink size={10} />
            </a>
            <i>·</i> POC STUDIO v0.3
          </span>
        </footer>
      </main>
      {historyOpen && (
        <div className="modal-backdrop" onClick={() => setHistoryOpen(false)}>
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-label="Saved broadcasts"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-title">
              <h2>Saved broadcasts</h2>
              <button
                className="icon-button"
                aria-label="Close saved broadcasts"
                onClick={() => setHistoryOpen(false)}
              >
                <X size={19} />
              </button>
            </div>
            <p>
              Inspect the original conversation and traces. Reopening a run never regenerates
              commentary.
            </p>
            <div className="saved-list">
              {saved.length ? (
                saved.map((run) => (
                  <button
                    key={run.id}
                    onClick={() => void viewSaved(run.id)}
                    disabled={busy || startingHighlight}
                  >
                    <div>
                      <History size={18} />
                      <span>
                        <strong>
                          {run.game
                            ? `${run.game.away_team} at ${run.game.home_team}`
                            : (run.game_id ?? run.id.slice(0, 8))}
                        </strong>
                        <small>
                          {run.created_at
                            ? new Date(run.created_at).toLocaleString()
                            : run.id.slice(0, 12)}{' '}
                          · {run.mode} · {run.provider}
                        </small>
                      </span>
                    </div>
                    <span>
                      {run.status} <ChevronRight size={15} />
                    </span>
                  </button>
                ))
              ) : (
                <div className="feed-empty">
                  <History size={24} />
                  <p>No saved broadcasts yet.</p>
                </div>
              )}
            </div>
          </section>
        </div>
      )}
      {help && (
        <div className="modal-backdrop" onClick={() => setHelp(false)}>
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-label="About BanterBots"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-title">
              <h2>Your commentary studio</h2>
              <button
                className="icon-button"
                aria-label="Close studio information"
                onClick={() => setHelp(false)}
              >
                <X size={19} />
              </button>
            </div>
            <p>
              Max backs the home team. Riley backs the visitors. Both see one frozen game snapshot,
              then trade a call and a reply through A2A.
            </p>
            <div className="help-step">
              <Play size={19} />
              <span>
                <strong>Enter the replay room</strong>Choose a real game and press Start broadcast.
                Use Next play to inspect a single exchange.
              </span>
            </div>
            <div className="help-step">
              <Volume2 size={19} />
              <span>
                <strong>Turn up the rivalry</strong>Enable voice. Demo commentary uses browser
                voices; OpenAI mode streams GPT-Live audio after credentials are configured.
              </span>
            </div>
            <div className="help-step">
              <Code2 size={19} />
              <span>
                <strong>Follow every handoff</strong>Open the debugger for graph stages, A2A
                payloads, prompt context, errors, and correlated traces.
              </span>
            </div>
            <div className="help-note">
              Live scores use ESPN’s polling feed. Poll time and latest-play time are reported
              separately. Demo speech is intentionally labelled.
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
export default App;
