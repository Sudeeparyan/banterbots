import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Megaphone, Mic, Send, ThumbsDown, Vote } from 'lucide-react';
import { post } from './types';
import type { RunEvent, Session, Snapshot } from './types';

type Side = 'home' | 'away' | 'neutral';
type Pick = 'convert' | 'stopped';
type Recognition = {
  lang: string;
  interimResults: boolean;
  maxAlternatives: number;
  onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null;
  onerror: (() => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
};
type RecognitionWindow = Window & {
  SpeechRecognition?: new () => Recognition;
  webkitSpeechRecognition?: new () => Recognition;
};

const ORDINAL: Record<number, string> = { 3: '3rd', 4: '4th' };

/** The latest prediction poll and, once its play has been shown, whether it converted. */
export function pollState(events: RunEvent[]) {
  const open = [...events].reverse().find((event) => event.type === 'poll.open');
  if (!open) return null;
  const result = events.find(
    (event) =>
      event.type === 'snapshot' &&
      event.seq > open.seq &&
      (event.data.snapshot as Snapshot | undefined)?.event?.id === open.data.event_id,
  );
  const flags = new Set((result?.data.snapshot as Snapshot | undefined)?.event.flags ?? []);
  const converted =
    (flags.has('first_down') || flags.has('touchdown')) &&
    !['reversed', 'no_play', 'play_deleted'].some((flag) => flags.has(flag));
  return {
    eventId: String(open.data.event_id),
    question: String(open.data.question ?? 'Do they convert?'),
    down: Number(open.data.down),
    distance: Number(open.data.distance),
    closed: Boolean(result),
    outcome: result ? ((converted ? 'convert' : 'stopped') as Pick) : null,
  };
}

/**
 * The listener's seat in the stadium: pick a side, cheer or boo, call the next
 * third or fourth down, and send the booth a question for the next break.
 * Everything typed here reaches the hosts as quoted data, never as instructions.
 */
export function ListenerPanel({
  session,
  disabled,
  homeName,
  awayName,
  onReaction,
}: {
  session: Session | null;
  disabled: boolean;
  homeName: string;
  awayName: string;
  onReaction: (kind: 'cheer' | 'boo') => void;
}) {
  const [side, setSide] = useState<Side | null>(session?.listener?.side ?? null);
  const [picks, setPicks] = useState<Record<string, Pick>>({});
  const [question, setQuestion] = useState('');
  const [notice, setNotice] = useState('');
  const [listening, setListening] = useState(false);
  const recognition = useRef<Recognition | null>(null);
  const id = session?.id;
  const poll = pollState(session?.events ?? []);
  const Speech =
    typeof window !== 'undefined'
      ? ((window as RecognitionWindow).SpeechRecognition ??
        (window as RecognitionWindow).webkitSpeechRecognition)
      : undefined;

  useEffect(() => {
    setSide(session?.listener?.side ?? null);
    setPicks({});
    setNotice('');
    // Only a new broadcast resets the seat; the server's view follows the listener's clicks.
  }, [id]);
  useEffect(() => () => recognition.current?.stop(), []);

  const send = async (body: Record<string, unknown>) => {
    if (!id) return null;
    try {
      const result = await post<Record<string, unknown>>(`/sessions/${id}/listener`, body);
      return result;
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error));
      return null;
    }
  };
  const chooseSide = (value: Side) => {
    setSide(value);
    void send({ side: value });
  };
  const react = (kind: 'cheer' | 'boo') => {
    onReaction(kind);
    void send({ reaction: kind });
  };
  const vote = (pick: Pick) => {
    if (!poll || poll.closed) return;
    setPicks((current) => ({ ...current, [poll.eventId]: pick }));
    void send({ poll_vote: pick, poll_event_id: poll.eventId });
  };
  const ask = async (event: FormEvent) => {
    event.preventDefault();
    const text = question.trim();
    if (text.length < 3) return;
    setNotice('');
    if (await send({ question: text })) {
      setQuestion('');
      setNotice('Sent. The booth answers at the next break.');
    }
  };
  const dictate = () => {
    if (!Speech) return;
    if (listening) {
      recognition.current?.stop();
      return;
    }
    const recognizer = new Speech();
    recognizer.lang = 'en-US';
    recognizer.interimResults = false;
    recognizer.maxAlternatives = 1;
    recognizer.onresult = (event) =>
      setQuestion(String(event.results[0]?.[0]?.transcript ?? '').slice(0, 200));
    recognizer.onerror = () =>
      setNotice('The browser could not hear that. Type your question instead.');
    recognizer.onend = () => setListening(false);
    recognition.current = recognizer;
    setListening(true);
    recognizer.start();
  };
  const picked = poll ? picks[poll.eventId] : undefined;
  const sides: [Side, string][] = [
    ['home', homeName],
    ['neutral', 'Neutral'],
    ['away', awayName],
  ];

  return (
    <section className="panel listener-panel" aria-label="Your seat">
      <div className="panel-header">
        <span>
          <Megaphone size={15} /> YOUR SEAT
        </span>
      </div>
      <div className="seat-sides" role="group" aria-label="Pick a side">
        {sides.map(([value, label]) => (
          <button
            key={value}
            className={`seat-side ${side === value ? 'chosen' : ''}`}
            aria-pressed={side === value}
            disabled={disabled}
            onClick={() => chooseSide(value)}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="seat-reactions">
        <button className="seat-cheer" disabled={disabled} onClick={() => react('cheer')}>
          <Megaphone size={14} /> Cheer
        </button>
        <button className="seat-boo" disabled={disabled} onClick={() => react('boo')}>
          <ThumbsDown size={14} /> Boo
        </button>
      </div>
      {poll && (
        <div className={`seat-poll ${poll.closed ? 'closed' : ''}`} aria-live="polite">
          <span className="eyebrow">
            <Vote size={13} /> {ORDINAL[poll.down] ?? `Down ${poll.down}`} and {poll.distance}
          </span>
          <p>{poll.question}</p>
          <div className="seat-poll-options">
            {(['convert', 'stopped'] as const).map((option) => (
              <button
                key={option}
                aria-pressed={picked === option}
                className={picked === option ? 'chosen' : ''}
                disabled={disabled || poll.closed}
                onClick={() => vote(option)}
              >
                {option === 'convert' ? 'They convert' : "They're stopped"}
              </button>
            ))}
          </div>
          {poll.closed && picked && (
            <small>{picked === poll.outcome ? 'You called it.' : 'Not this time.'}</small>
          )}
        </div>
      )}
      <form className="seat-ask" onSubmit={(event) => void ask(event)}>
        <label htmlFor="ask-booth">Ask the booth</label>
        <div>
          <input
            id="ask-booth"
            value={question}
            maxLength={200}
            placeholder="Who has more yards so far?"
            disabled={disabled}
            onChange={(event) => setQuestion(event.target.value)}
          />
          {Speech && (
            <button
              type="button"
              aria-label={listening ? 'Stop dictation' : 'Dictate a question'}
              aria-pressed={listening}
              disabled={disabled}
              onClick={dictate}
            >
              <Mic size={14} />
            </button>
          )}
          <button
            type="submit"
            aria-label="Send question"
            disabled={disabled || question.trim().length < 3}
          >
            <Send size={14} />
          </button>
        </div>
        {Speech && (
          <small>
            Dictation uses your browser&apos;s speech service, which may process audio in the cloud.
          </small>
        )}
        {notice && <small role="status">{notice}</small>}
      </form>
    </section>
  );
}
