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
  private pendingAck = new Set<string>();
  private settled = new Set<string>();

  constructor(
    private speaking: (agent: string | null) => void,
    private ack: (id: string, samples: number, cancelled: boolean) => void,
  ) {}

  async unlock() {
    const generation = this.generation;
    try {
      this.context ??= new AudioContext({ sampleRate: 24000 });
      await this.context.resume();
      if (generation !== this.generation) return;
      this.muted = false;
      this.pump();
      this.pumpDemo();
    } catch (error) {
      if (generation !== this.generation) return;
      this.muted = true;
      this.clear();
      throw error;
    }
  }

  mute(value: boolean) {
    this.muted = value;
    if (value) this.clear();
  }

  clear() {
    this.generation++;
    if (this.timer) clearTimeout(this.timer);
    this.timer = undefined;
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
    if ('speechSynthesis' in window) window.speechSynthesis.cancel();
    this.speaking(null);
  }

  chunk(chunk: Chunk) {
    if (this.settled.has(chunk.segment_id) || this.pendingAck.has(chunk.segment_id)) return;
    if (this.muted) {
      this.pendingAck.add(chunk.segment_id);
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
    if (chunk.chunk_index >= segment.next && !segment.chunks.has(chunk.chunk_index)) {
      try {
        const rate = chunk.rate ?? 24000;
        if (!Number.isFinite(rate) || rate < 8000 || rate > 192000)
          throw new Error('Invalid PCM sample rate');
        const pcm = decodePCM(chunk.audio);
        if (pcm.length) segment.chunks.set(chunk.chunk_index, { pcm, rate });
        else throw new Error('Empty PCM chunk');
      } catch {
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
    this.ack(id, samples, cancelled);
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
      if ('speechSynthesis' in window) window.speechSynthesis.cancel();
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
      const buffer = context.createBuffer(1, pcm.length, rate);
      buffer.copyToChannel(pcm as Float32Array<ArrayBuffer>, 0);
      const source = context.createBufferSource();
      source.buffer = buffer;
      source.connect(context.destination);
      const when = Math.max(context.currentTime + 0.015, segment.scheduled);
      segment.scheduled = when + buffer.duration;
      segment.scheduledSamples += pcm.length;
      segment.clips.push({ start: when, samples: pcm.length, rate });
      segment.sources.push(source);
      source.start(when);
      this.speaking(segment.agent);
    }
    if (segment.sealed && segment.scheduledSamples >= segment.total) {
      if (this.timer) clearTimeout(this.timer);
      const generation = this.generation;
      const complete = () => {
        if (generation !== this.generation || this.segments[0] !== segment) return;
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
    if (this.muted || !('speechSynthesis' in window)) {
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
    const utterance = new SpeechSynthesisUtterance(item.text);
    const voices = window.speechSynthesis
      .getVoices()
      .filter((voice) => voice.lang.startsWith('en'));
    utterance.voice = voices[item.agent === 'a' ? 0 : Math.min(1, voices.length - 1)] ?? null;
    utterance.rate = item.agent === 'a' ? 1.09 : 0.98;
    utterance.pitch = item.agent === 'a' ? 1.03 : 0.86;
    const done = (cancelled: boolean) => {
      if (generation !== this.generation || this.activeDemo !== item) return;
      this.finish(item.id, 0, cancelled);
      this.activeDemo = null;
      this.speaking(null);
      this.pump();
      this.pumpDemo();
    };
    utterance.onstart = () => {
      if (generation === this.generation && this.activeDemo === item) this.speaking(item.agent);
    };
    utterance.onend = () => done(false);
    utterance.onerror = () => done(true);
    try {
      window.speechSynthesis.speak(utterance);
    } catch {
      done(true);
    }
  }
}
