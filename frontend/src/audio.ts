import { BroadcastSoundscape, browserDelivery, createMaster, speechPhrases } from './soundscape';
import type { VocalDelivery } from './types';

type Chunk = {
  segment_id: string;
  agent_id: string;
  chunk_index: number;
  audio: string;
  rate?: number;
};
type Seal = {
  segment_id: string;
  total_samples: number;
  agent_id?: string;
  discard_audio?: boolean;
};
type Clip = { start: number; samples: number; rate: number };
type Segment = {
  id: string;
  agent: string;
  chunks: Map<number, { pcm: Float32Array; rate: number }>;
  next: number;
  sealed: boolean;
  total: number;
  scheduledSamples: number;
  scheduled: number;
  sources: AudioBufferSourceNode[];
  clips: Clip[];
  started: boolean;
  reacted: boolean;
  // Jumped in over the previous speaker's last words: no breath before it.
  overlap: boolean;
};
type Demo = {
  text: string;
  agent: string;
  id: string;
  delivery?: VocalDelivery | null;
  eventKey: string;
};
type Prepared = { delivery?: VocalDelivery | null; eventKey: string };

type Mark = Record<string, string | number | boolean | null>;

/** The longest a partner may talk over the end of the previous line. */
const MAX_OVERLAP = 0.4;

/** Browser voices whose names mark the higher-quality neural engines Edge and Chrome expose. */
const NATURAL_VOICE = /natural|neural|google/i;

/** Opt-in playback timing for listening tests; inert unless a harness installs the array. */
export function mark(entry: Mark) {
  const timeline = (globalThis as { __banterTimeline?: unknown }).__banterTimeline;
  if (Array.isArray(timeline)) timeline.push({ ...entry, at: performance.now() });
}

/** First and last 10 ms windows that carry speech energy, in seconds from the chunk start. */
export function audibleBounds(pcm: Float32Array, rate: number): [number, number] | null {
  const window = Math.max(1, Math.round(rate / 100));
  let first = -1,
    last = -1;
  for (let start = 0; start < pcm.length; start += window) {
    let sum = 0;
    const end = Math.min(pcm.length, start + window);
    for (let i = start; i < end; i++) sum += pcm[i] * pcm[i];
    if (Math.sqrt(sum / (end - start)) >= 0.006) {
      if (first < 0) first = start;
      last = end;
    }
  }
  return first < 0 ? null : [first / rate, last / rate];
}

export function decodePCM(base64: string): Float32Array {
  const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
  if (bytes.length % 2) throw new Error('PCM16 audio contains an incomplete sample');
  const view = new DataView(bytes.buffer);
  const samples = new Float32Array(bytes.length / 2);
  for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
  return samples;
}

export class BroadcastAudio {
  private context: AudioContext | null = null;
  private segments: Segment[] = [];
  private muted = true;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private generation = 0;
  private speakQueue: Demo[] = [];
  private activeDemo: Demo | null = null;
  // Keep the utterance alive until the speech engine reports completion.
  private activeUtterance: SpeechSynthesisUtterance | null = null;
  private demoTimer: ReturnType<typeof setTimeout> | undefined;
  private previewSequence = 0;
  private previewIds = new Set<string>();
  private pendingAck = new Set<string>();
  private settled = new Set<string>();
  private prepared = new Map<string, Prepared>();
  private soundscape: BroadcastSoundscape | null = null;
  private soundscapeEnabled = false;
  private soundscapeLevel = 0.35;
  private broadcastActive = false;
  private sounding = false;
  private sceneVoiceActive = false;
  private sceneFailed = false;
  // The last moments of a line still playing under an overlapping partner.
  private tails = new Set<AudioBufferSourceNode>();
  // Loudness make-up and peak limiting for voice and crowd alike.
  private master: AudioNode | null = null;

  constructor(
    private speaking: (agent: string | null) => void,
    private ack: (id: string, samples: number, cancelled: boolean) => void,
    private onIssue: (message: string, muted: boolean) => void = () => {},
  ) {}

  async unlock() {
    const generation = this.generation;
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      const Context =
        typeof AudioContext !== 'undefined'
          ? AudioContext
          : (window as Window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (this.context?.state === 'closed') {
        this.context = null;
        this.master = null;
        this.soundscape = null;
        this.sceneFailed = false;
      }
      // Buffers specify their own PCM rate. Let the browser choose the output
      // device rate; forcing 24 kHz rejects otherwise usable audio devices.
      if (!this.context && Context) this.context = new Context();
      if (this.context && !this.master) {
        try {
          this.master = createMaster(this.context);
        } catch {
          this.master = null; // a device without dynamics processing still plays the voice
        }
      }
      if (this.context) {
        await Promise.race([
          this.context.resume(),
          new Promise<never>((_, reject) => {
            timer = setTimeout(
              () => reject(new Error('The browser did not enable audio in time')),
              8000,
            );
          }),
        ]);
      } else if (!this.hasSpeech()) {
        throw new Error('This browser does not support spoken commentary');
      }
      if (generation !== this.generation) return;
      this.muted = false;
      this.updateSoundscape();
      this.pump();
      this.pumpDemo();
    } catch (error) {
      if (generation !== this.generation) return;
      this.muted = true;
      this.clear();
      throw error;
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  mute(value: boolean) {
    this.muted = value;
    if (value) this.clear();
  }

  setSoundscape(enabled: boolean, level = 0.35) {
    this.soundscapeEnabled = enabled;
    this.soundscapeLevel = Number.isFinite(level) ? Math.max(0, Math.min(1, level)) : 0.35;
    if (enabled) this.sceneFailed = false;
    this.updateSoundscape();
  }

  setBroadcastActive(active: boolean) {
    this.broadcastActive = active;
    this.updateSoundscape();
  }

  /** The segment is queued or playing: not yet heard to its end, nor dropped. */
  pending(id: string): boolean {
    if (this.settled.has(id)) return false;
    return (
      this.segments.some((segment) => segment.id === id) ||
      this.activeDemo?.id === id ||
      this.speakQueue.some((item) => item.id === id)
    );
  }

  /** The listener's cheer or boo, played at once in their section; false when the crowd is off. */
  listenerReaction(kind: 'cheer' | 'boo'): boolean {
    if (this.muted || !this.soundscape) return false;
    try {
      return this.soundscape.listener(kind);
    } catch {
      return false;
    }
  }

  /** Turn metadata arrives before either streamed PCM or local browser speech. */
  prepare(id: string, eventKey: string, delivery?: VocalDelivery | null) {
    if (this.settled.has(id)) return;
    this.prepared.set(id, { eventKey, delivery });
    if (this.prepared.size > 1024) this.prepared.delete(this.prepared.keys().next().value!);
  }

  private updateSoundscape() {
    if (this.sceneFailed || !this.context) return;
    try {
      if (!this.soundscape && this.soundscapeEnabled)
        this.soundscape = new BroadcastSoundscape(this.context, this.master ?? undefined);
      if (!this.soundscape) return;
      this.soundscape.setLevel(this.soundscapeLevel);
      this.soundscape.setEnabled(this.soundscapeEnabled && !this.muted);
      this.soundscape.setActive(!this.muted && (this.broadcastActive || this.sceneVoiceActive));
      this.soundscape.setSpeaking(this.sounding);
    } catch {
      // Optional atmosphere must never prevent the commentary itself playing.
      this.sceneFailed = true;
      try {
        this.soundscape?.stop();
      } catch {
        /* failed audio graph */
      }
      this.soundscape = null;
      this.issue(
        'Stadium sounds are unavailable on this audio device. Commentary voice remains available.',
      );
    }
  }

  private voiceState(agent: string | null, atmosphere = true) {
    this.sounding = agent !== null;
    this.sceneVoiceActive = this.sounding && atmosphere;
    this.updateSoundscape();
    this.speaking(agent);
  }

  private reaction(id: string, when?: number) {
    const plan = this.prepared.get(id);
    if (!plan || this.previewIds.has(id)) return;
    try {
      this.soundscape?.cue(plan.eventKey, plan.delivery, when);
    } catch {
      this.issue('A stadium reaction was skipped. Commentary voice remains available.');
    }
  }

  clear() {
    this.generation++;
    if (this.timer) clearTimeout(this.timer);
    if (this.demoTimer) clearTimeout(this.demoTimer);
    this.timer = undefined;
    this.demoTimer = undefined;
    for (const segment of this.segments) {
      const played = this.playedSamples(segment);
      this.stopSources(segment);
      this.finish(segment.id, played, true);
    }
    if (this.activeDemo) this.finish(this.activeDemo.id, 0, true);
    for (const item of this.speakQueue) this.finish(item.id, 0, true);
    for (const id of this.pendingAck) this.finish(id, 0, true);
    this.pendingAck.clear();
    for (const source of this.tails) {
      try {
        source.stop();
      } catch {
        /* tail already finished */
      }
    }
    this.tails.clear();
    this.segments = [];
    this.speakQueue = [];
    this.activeDemo = null;
    this.activeUtterance = null;
    this.cancelSpeech();
    this.sounding = false;
    this.sceneVoiceActive = false;
    try {
      this.soundscape?.stop();
    } catch {
      /* a failed effect cannot block voice cleanup */
    }
    this.prepared.clear();
    this.speaking(null);
  }

  chunk(chunk: Chunk) {
    if (this.settled.has(chunk.segment_id) || this.pendingAck.has(chunk.segment_id)) return;
    if (this.muted) {
      this.pendingAck.add(chunk.segment_id);
      return;
    }
    if (!this.context) {
      this.issue(
        'Streaming audio is unavailable in this browser. Commentary captions remain available.',
      );
      this.discard(chunk.segment_id);
      return;
    }
    if (!Number.isInteger(chunk.chunk_index) || chunk.chunk_index < 0) {
      this.discard(chunk.segment_id);
      return;
    }
    let segment = this.segments.find((item) => item.id === chunk.segment_id);
    if (!segment) {
      segment = this.newSegment(chunk.segment_id, chunk.agent_id);
      this.segments.push(segment);
    }
    if (
      segment.sealed &&
      chunk.chunk_index >= segment.next &&
      !segment.chunks.has(chunk.chunk_index)
    ) {
      this.issue('Audio arrived after its segment was sealed and was skipped.');
      this.discard(chunk.segment_id);
      return;
    }
    if (chunk.chunk_index >= segment.next && !segment.chunks.has(chunk.chunk_index)) {
      try {
        const rate = chunk.rate ?? 24000;
        if (!Number.isFinite(rate) || rate < 8000 || rate > 192000)
          throw new Error('Invalid PCM sample rate');
        const pcm = decodePCM(chunk.audio);
        if (pcm.length) segment.chunks.set(chunk.chunk_index, { pcm, rate });
        else throw new Error('Empty PCM chunk');
      } catch {
        this.issue('A commentary audio chunk was invalid and was skipped.');
        this.discard(chunk.segment_id);
        return;
      }
    }
    this.pump();
  }

  seal(seal: Seal) {
    if (this.settled.has(seal.segment_id)) return;
    if (
      seal.discard_audio ||
      this.muted ||
      this.pendingAck.has(seal.segment_id) ||
      !Number.isInteger(seal.total_samples) ||
      seal.total_samples < 0
    ) {
      this.discard(seal.segment_id);
      return;
    }
    let segment = this.segments.find((item) => item.id === seal.segment_id);
    if (!segment) {
      segment = this.newSegment(seal.segment_id, seal.agent_id ?? 'a');
      this.segments.push(segment);
    }
    const received =
      segment.scheduledSamples +
      [...segment.chunks.values()].reduce((sum, chunk) => sum + chunk.pcm.length, 0);
    const contiguous = Array.from({ length: segment.chunks.size }, (_, index) =>
      segment!.chunks.has(segment!.next + index),
    ).every(Boolean);
    // A seal is the end of the ordered stream, so missing PCM cannot arrive
    // later. Cancel incomplete segments instead of blocking the whole booth.
    if (received !== seal.total_samples || !contiguous) {
      this.discard(seal.segment_id);
      return;
    }
    segment.sealed = true;
    segment.total = seal.total_samples;
    this.pump();
  }

  private newSegment(id: string, agent: string): Segment {
    return {
      id,
      agent,
      chunks: new Map(),
      next: 0,
      sealed: false,
      total: 0,
      scheduledSamples: 0,
      scheduled: 0,
      sources: [],
      clips: [],
      started: false,
      reacted: false,
      overlap: false,
    };
  }

  private finish(id: string, samples: number, cancelled: boolean) {
    if (this.settled.has(id)) return;
    this.settled.add(id);
    // Bound the ledger while retaining recent completions and cancellations.
    if (this.settled.size > 1024) this.settled.delete(this.settled.values().next().value!);
    this.pendingAck.delete(id);
    this.prepared.delete(id);
    mark({ type: 'segment_end', segment: id, cancelled, samples });
    if (this.previewIds.delete(id)) return;
    try {
      this.ack(id, samples, cancelled);
    } catch {
      this.issue('The audio acknowledgement could not be sent. Reconnect to the broadcast.');
    }
  }

  private issue(message: string) {
    try {
      this.onIssue(message, this.muted);
    } catch {
      /* A display callback cannot interrupt playback cleanup. */
    }
  }

  private hasSpeech(): boolean {
    return (
      typeof SpeechSynthesisUtterance !== 'undefined' &&
      typeof window.speechSynthesis?.speak === 'function'
    );
  }

  private cancelSpeech(): boolean {
    try {
      window.speechSynthesis?.cancel();
      return true;
    } catch {
      // A broken cancellation API must not let another voice overlap this one.
      this.muted = true;
      this.issue('The browser could not stop its speech engine. Voice has been muted.');
      return false;
    }
  }

  private playedSamples(segment: Segment): number {
    const now = this.context?.currentTime ?? 0;
    return segment.clips.reduce(
      (total, clip) =>
        total + Math.min(clip.samples, Math.max(0, Math.floor((now - clip.start) * clip.rate))),
      0,
    );
  }

  private stopSources(segment: Segment) {
    for (const source of segment.sources) {
      try {
        source.stop();
      } catch {
        /* source already finished */
      }
    }
  }

  private discard(id: string) {
    if (this.settled.has(id)) return;
    if (this.activeDemo?.id === id) {
      this.generation++;
      this.activeDemo = null;
      this.activeUtterance = null;
      if (this.demoTimer) clearTimeout(this.demoTimer);
      this.demoTimer = undefined;
      if (!this.cancelSpeech()) this.clear();
      this.voiceState(null);
    }
    this.speakQueue = this.speakQueue.filter((item) => item.id !== id);
    const index = this.segments.findIndex((segment) => segment.id === id);
    if (index >= 0) {
      const [segment] = this.segments.splice(index, 1);
      const played = this.playedSamples(segment);
      this.stopSources(segment);
      if (index === 0 && this.timer) {
        clearTimeout(this.timer);
        this.timer = undefined;
      }
      this.finish(id, played, true);
      if (index === 0) this.voiceState(null);
    } else {
      this.finish(id, 0, true);
    }
    this.pump();
    this.pumpDemo();
  }

  private pump() {
    const context = this.context,
      segment = this.segments[0];
    if (!context || !segment || this.muted || this.activeDemo) return;
    while (segment.chunks.has(segment.next)) {
      const { pcm, rate } = segment.chunks.get(segment.next)!;
      segment.chunks.delete(segment.next++);
      try {
        const buffer = context.createBuffer(1, pcm.length, rate);
        buffer.copyToChannel(pcm as Float32Array<ArrayBuffer>, 0);
        const source = context.createBufferSource();
        segment.sources.push(source);
        source.buffer = buffer;
        const plan = this.prepared.get(segment.id);
        const pause =
          segment.started || segment.overlap
            ? 0
            : browserDelivery(segment.agent, plan?.delivery).pause / 1000 +
              this.stingerLead(segment.id, plan?.delivery);
        this.voiceState(segment.agent);
        // Building the optional atmosphere can take time on a slow device.
        // Measure after that work so sample accounting matches the actual start.
        const when = Math.max(context.currentTime + Math.max(0.015, pause), segment.scheduled);
        source.connect(this.soundscape?.voiceDestination ?? this.master ?? context.destination);
        source.start(when);
        if (Array.isArray((globalThis as { __banterTimeline?: unknown }).__banterTimeline)) {
          const bounds = audibleBounds(pcm, rate),
            offset = performance.now() + (when - context.currentTime) * 1000;
          mark({
            type: 'voice',
            segment: segment.id,
            agent: segment.agent,
            start_ms: offset,
            duration_ms: buffer.duration * 1000,
            first_ms: bounds ? offset + bounds[0] * 1000 : null,
            last_ms: bounds ? offset + bounds[1] * 1000 : null,
          });
        }
        if (!segment.reacted) {
          const onset = pcm.findIndex((sample) => Math.abs(sample) >= 0.002);
          if (onset >= 0) {
            this.reaction(segment.id, when + onset / rate);
            segment.reacted = true;
          }
        }
        segment.started = true;
        segment.scheduled = when + buffer.duration;
        segment.scheduledSamples += pcm.length;
        segment.clips.push({ start: when, samples: pcm.length, rate });
      } catch {
        this.issue(
          'The browser could not play this commentary segment. Captions remain available.',
        );
        this.discard(segment.id);
        return;
      }
    }
    if (segment.sealed && segment.scheduledSamples >= segment.total) {
      if (this.timer) clearTimeout(this.timer);
      const generation = this.generation;
      const complete = () => {
        if (generation !== this.generation || this.segments[0] !== segment) return;
        if (context.state === 'closed') {
          this.issue('The browser audio device closed during playback. This segment was skipped.');
          this.discard(segment.id);
          return;
        }
        // AudioContext time stops when a browser suspends playback. Wall-clock
        // timers alone would release the peer before the lead was heard.
        const remaining = segment.scheduled - context.currentTime,
          overlap = this.overlapFor(segment);
        if (remaining > overlap) {
          this.timer = setTimeout(
            complete,
            Math.max(20, (remaining - overlap) * 1000 + (overlap ? 0 : 15)),
          );
          return;
        }
        this.timer = undefined;
        this.finish(segment.id, segment.scheduledSamples, false);
        this.segments.shift();
        if (remaining > 0) {
          // The partner jumps in on the last word; pause and mute still stop this tail.
          this.segments[0].overlap = true;
          for (const source of segment.sources) {
            this.tails.add(source);
            source.onended = () => this.tails.delete(source);
          }
          mark({ type: 'overlap', segment: this.segments[0].id, ms: Math.round(remaining * 1000) });
        } else this.voiceState(null);
        this.pump();
        this.pumpDemo();
      };
      // Wake early enough to let an overlapping partner in; otherwise wait for the end.
      this.timer = setTimeout(
        complete,
        Math.max(0, (segment.scheduled - context.currentTime - MAX_OVERLAP) * 1000) + 15,
      );
    }
  }

  /** Open a segment with its stinger, once; returns how long its voice waits for the tail. */
  private stingerLead(id: string, delivery?: VocalDelivery | null): number {
    const kind = delivery?.stinger;
    if (!kind || kind === 'none' || this.previewIds.has(id)) return 0;
    try {
      return this.soundscape?.stinger(kind) ?? 0;
    } catch {
      return 0;
    }
  }

  /** Seconds the next speaker may start early: only a different host, already buffered, on a big moment. */
  private overlapFor(segment: Segment): number {
    const next = this.segments[1];
    if (!next || next.agent === segment.agent || !next.chunks.has(next.next)) return 0;
    const ms = this.prepared.get(next.id)?.delivery?.overlap_ms ?? 0;
    return Number.isFinite(ms) ? Math.max(0, Math.min(MAX_OVERLAP, ms / 1000)) : 0;
  }

  async testVoice() {
    await this.unlock();
    const id = `voice-preview:${++this.previewSequence}`;
    this.previewIds.add(id);
    this.demo('Commentary voice is ready.', 'a', id);
  }

  demo(
    text: string,
    agent: string,
    id: string,
    delivery?: VocalDelivery | null,
    eventKey?: string,
  ) {
    if (
      this.settled.has(id) ||
      this.activeDemo?.id === id ||
      this.speakQueue.some((item) => item.id === id)
    )
      return;
    if (this.muted) {
      this.finish(id, 0, true);
      return;
    }
    if (!this.hasSpeech()) {
      this.muted = true;
      this.issue(
        'This browser does not support spoken commentary. Open the studio in Chrome or Edge and enable Voice.',
      );
      this.finish(id, 0, true);
      this.clear();
      return;
    }
    if (delivery !== undefined)
      this.prepare(id, eventKey ?? this.prepared.get(id)?.eventKey ?? id, delivery);
    const plan = this.prepared.get(id);
    this.speakQueue.push({
      text,
      agent,
      id,
      delivery: plan?.delivery,
      eventKey: plan?.eventKey ?? eventKey ?? id,
    });
    this.pumpDemo();
  }

  private pumpDemo() {
    if (this.activeDemo || this.muted || this.segments.length) return;
    const item = this.speakQueue.shift();
    if (!item) return;
    this.activeDemo = item;
    const generation = this.generation;
    const done = (cancelled: boolean) => {
      if (generation !== this.generation || this.activeDemo !== item) return;
      if (this.demoTimer) clearTimeout(this.demoTimer);
      this.demoTimer = undefined;
      this.finish(item.id, 0, cancelled);
      this.activeDemo = null;
      this.activeUtterance = null;
      this.voiceState(null);
      this.pump();
      this.pumpDemo();
    };
    const watchdog = () => {
      if (generation !== this.generation || this.activeDemo !== item) return;
      this.issue(
        'Browser speech stopped responding. This line was skipped; captions remain available.',
      );
      this.discard(item.id);
    };
    const phrases = speechPhrases(item.text, item.delivery);
    let started = false;
    const speakPhrase = (index: number) => {
      if (generation !== this.generation || this.activeDemo !== item || this.muted) return;
      try {
        const utterance = new SpeechSynthesisUtterance(phrases[index]);
        this.activeUtterance = utterance;
        utterance.lang = 'en-US';
        try {
          const english = window.speechSynthesis
            .getVoices()
            .filter((voice) => /^en(?:[-_]|$)/i.test(voice.lang));
          // Prefer the browser's natural neural voices (Edge and Chrome stream them);
          // otherwise both commentators work offline, even with one installed
          // voice. Rate and pitch still distinguish their delivery.
          const natural = english.filter((voice) => NATURAL_VOICE.test(voice.name ?? '')),
            installed = english.filter((voice) => voice.localService),
            voices = natural.length ? natural : installed.length ? installed : english;
          utterance.voice = voices[item.agent === 'a' ? 0 : Math.min(1, voices.length - 1)] ?? null;
          if (utterance.voice) utterance.lang = utterance.voice.lang;
        } catch {
          this.issue('The browser voice list is unavailable. Its default voice will be used.');
        }
        const delivery = browserDelivery(item.agent, item.delivery, index);
        utterance.rate = delivery.rate;
        utterance.pitch = delivery.pitch;
        utterance.volume = delivery.volume;
        utterance.onstart = () => {
          if (
            generation !== this.generation ||
            this.activeDemo !== item ||
            this.activeUtterance !== utterance
          )
            return;
          this.voiceState(item.agent, !this.previewIds.has(item.id));
          mark({ type: 'speech_start', segment: item.id, agent: item.agent, phrase: index });
          if (!started) {
            this.reaction(item.id);
            started = true;
          }
          if (this.demoTimer) clearTimeout(this.demoTimer);
          const duration = Math.min(
            25000,
            Math.max(10000, phrases[index].trim().split(/\s+/).length * 700 + 5000),
          );
          this.demoTimer = setTimeout(watchdog, duration);
        };
        utterance.onend = () => {
          if (
            generation !== this.generation ||
            this.activeDemo !== item ||
            this.activeUtterance !== utterance
          )
            return;
          mark({ type: 'speech_end', segment: item.id, agent: item.agent, phrase: index });
          if (index === phrases.length - 1) {
            done(false);
            return;
          }
          if (this.demoTimer) clearTimeout(this.demoTimer);
          this.activeUtterance = null;
          // A small breath between reaction and explanation, retaining the floor.
          this.demoTimer = setTimeout(() => speakPhrase(index + 1), 60);
        };
        utterance.onerror = (event) => {
          if (
            generation !== this.generation ||
            this.activeDemo !== item ||
            this.activeUtterance !== utterance
          )
            return;
          if (event?.error === 'not-allowed') {
            this.muted = true;
            this.issue(
              'Your browser blocked spoken commentary. Allow sound for this site, then click Voice to enable it again.',
            );
            this.clear();
            return;
          }
          if (
            ['voice-unavailable', 'language-unavailable', 'synthesis-unavailable'].includes(
              event?.error ?? '',
            )
          ) {
            this.muted = true;
            this.issue(
              'The browser has no usable commentary voice. Enable an English speech voice in your browser or operating system, then click Voice to retry.',
            );
            this.clear();
            return;
          }
          if (event?.error === 'audio-hardware') {
            this.muted = true;
            this.issue(
              'The browser could not access your audio device. Check your output device, then click Voice to retry.',
            );
            this.clear();
            return;
          }
          this.issue('The browser speech engine skipped this line. Captions remain available.');
          done(true);
        };
        this.demoTimer = setTimeout(watchdog, 8000);
        window.speechSynthesis.speak(utterance);
        // Chromium's idle resume is a no-op, and its paused getter can miss an
        // idle native pause. Enqueue first, then wake the engine for this line.
        if (this.activeDemo === item && typeof window.speechSynthesis.resume === 'function')
          window.speechSynthesis.resume();
      } catch {
        this.issue('The browser could not speak this line. Commentary captions remain available.');
        this.discard(item.id);
      }
    };
    const pause =
      browserDelivery(item.agent, item.delivery).pause +
      this.stingerLead(item.id, item.delivery) * 1000;
    if (pause) this.demoTimer = setTimeout(() => speakPhrase(0), pause);
    else speakPhrase(0);
  }
}
