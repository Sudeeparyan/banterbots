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
};
type Demo = { text: string; agent: string; id: string };

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
  private demoTimer: ReturnType<typeof setTimeout> | undefined;
  private pendingAck = new Set<string>();
  private settled = new Set<string>();

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
      if (this.context?.state === 'closed') this.context = null;
      // Buffers specify their own PCM rate. Let the browser choose the output
      // device rate; forcing 24 kHz rejects otherwise usable audio devices.
      if (!this.context && Context) this.context = new Context();
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
    this.segments = [];
    this.speakQueue = [];
    this.activeDemo = null;
    this.cancelSpeech();
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
    };
  }

  private finish(id: string, samples: number, cancelled: boolean) {
    if (this.settled.has(id)) return;
    this.settled.add(id);
    // Bound the ledger while retaining recent completions and cancellations.
    if (this.settled.size > 1024) this.settled.delete(this.settled.values().next().value!);
    this.pendingAck.delete(id);
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
      if (this.demoTimer) clearTimeout(this.demoTimer);
      this.demoTimer = undefined;
      if (!this.cancelSpeech()) this.clear();
      this.speaking(null);
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
      if (index === 0) this.speaking(null);
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
        source.connect(context.destination);
        const when = Math.max(context.currentTime + 0.015, segment.scheduled);
        source.start(when);
        segment.scheduled = when + buffer.duration;
        segment.scheduledSamples += pcm.length;
        segment.clips.push({ start: when, samples: pcm.length, rate });
        this.speaking(segment.agent);
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
        const remaining = segment.scheduled - context.currentTime;
        if (remaining > 0) {
          this.timer = setTimeout(complete, Math.max(20, remaining * 1000 + 15));
          return;
        }
        this.timer = undefined;
        this.finish(segment.id, segment.scheduledSamples, false);
        this.segments.shift();
        this.speaking(null);
        this.pump();
        this.pumpDemo();
      };
      this.timer = setTimeout(
        complete,
        Math.max(0, (segment.scheduled - context.currentTime) * 1000) + 15,
      );
    }
  }

  demo(text: string, agent: string, id: string) {
    if (
      this.settled.has(id) ||
      this.activeDemo?.id === id ||
      this.speakQueue.some((item) => item.id === id)
    )
      return;
    if (this.muted || !this.hasSpeech()) {
      this.finish(id, 0, true);
      return;
    }
    this.speakQueue.push({ text, agent, id });
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
      this.speaking(null);
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
    try {
      const utterance = new SpeechSynthesisUtterance(item.text);
      try {
        const voices = window.speechSynthesis
          .getVoices()
          .filter((voice) => voice.lang.startsWith('en'));
        utterance.voice = voices[item.agent === 'a' ? 0 : Math.min(1, voices.length - 1)] ?? null;
      } catch {
        this.issue('The browser voice list is unavailable. Its default voice will be used.');
      }
      utterance.rate = item.agent === 'a' ? 1.09 : 0.98;
      utterance.pitch = item.agent === 'a' ? 1.03 : 0.86;
      utterance.onstart = () => {
        if (generation !== this.generation || this.activeDemo !== item) return;
        this.speaking(item.agent);
        if (this.demoTimer) clearTimeout(this.demoTimer);
        const duration = Math.min(
          25000,
          Math.max(10000, item.text.trim().split(/\s+/).length * 700 + 5000),
        );
        this.demoTimer = setTimeout(watchdog, duration);
      };
      utterance.onend = () => done(false);
      utterance.onerror = () => {
        if (generation !== this.generation || this.activeDemo !== item) return;
        this.issue('The browser speech engine skipped this line. Captions remain available.');
        done(true);
      };
      this.demoTimer = setTimeout(watchdog, 8000);
      window.speechSynthesis.speak(utterance);
    } catch {
      this.issue('The browser could not speak this line. Commentary captions remain available.');
      this.discard(item.id);
    }
  }
}
