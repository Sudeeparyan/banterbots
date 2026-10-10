import type { VocalDelivery } from './types';

const finite = (value: number, fallback: number, low: number, high: number) =>
  Number.isFinite(value) ? Math.max(low, Math.min(high, value)) : fallback;

/** Bounded adjustments rather than extreme pitch shifts on installed voices. */
export function browserDelivery(agent: string, delivery?: VocalDelivery | null, phrase = 0) {
  let rate = finite(delivery?.browser_rate ?? NaN, agent === 'a' ? 1.035 : 0.99, 0.85, 1.2);
  let pitch = finite(delivery?.browser_pitch ?? NaN, agent === 'a' ? 1.02 : 0.97, 0.85, 1.2);
  const contour = delivery?.contour;
  if (contour === 'lift_and_settle' || contour === 'explode') {
    rate *= phrase === 0 ? (contour === 'explode' ? 1.04 : 0.98) : 1.02;
    pitch += phrase === 0 ? (contour === 'explode' ? 0.06 : 0.035) : -0.025;
  } else if (contour === 'stunned_drop' || contour === 'groan') {
    rate *= phrase === 0 ? 0.95 : 0.98;
    pitch += phrase === 0 ? 0.025 : -0.035;
  } else if (contour === 'dry_lilt') {
    pitch += phrase === 0 ? 0.025 : -0.02;
  } else if (contour === 'suspense' || contour === 'build') {
    rate *= phrase === 0 ? 0.95 : 1.01;
    pitch += phrase === 0 ? -0.01 : 0.025;
  } else if (contour === 'warm_close') {
    rate *= 0.98;
    pitch += phrase === 0 ? 0.01 : -0.015;
  }
  return {
    rate: finite(rate, 1, 0.85, 1.2),
    pitch: finite(pitch, 1, 0.85, 1.2),
    volume: finite(delivery?.browser_volume ?? NaN, 0.95, 0.7, 1),
    pause: finite(delivery?.pause_before_ms ?? NaN, 0, 0, 600),
  };
}

/** Only expressive plans split sentences; legacy/browser previews remain one utterance. */
export function speechPhrases(text: string, delivery?: VocalDelivery | null): string[] {
  if (!delivery || delivery.contour === 'steady') return [text];
  const phrases: string[] = [];
  let start = 0;
  for (const boundary of text.matchAll(/[.!?]["'”’]?\s+(?=[A-Z])/g)) {
    const end = boundary.index + 1,
      preceding = text.slice(0, end);
    // Initials and common name abbreviations must stay attached to the player.
    if (/\b(?:[A-Za-z](?:\.[A-Za-z])+|[A-Z]|Mr|Mrs|Ms|Dr|St|Jr|Sr)\.$/.test(preceding)) continue;
    const whitespace = boundary[0].search(/\s/);
    phrases.push(text.slice(start, boundary.index + whitespace).trim());
    start = boundary.index + boundary[0].length;
  }
  phrases.push(text.slice(start).trim());
  if (phrases.length <= 3) return phrases;
  return [phrases[0], phrases[1], phrases.slice(2).join(' ')];
}

/**
 * Make-up gain, a peak limiter and a soft clip: the finished mix lands near -16 LUFS
 * with peaks under -1 dBTP (measured on the recorded scenarios, docs/VALIDATION.md).
 */
export const MASTER_GAIN_DB = 4;
const CLIP_CEILING = 0.85;

export function createMaster(context: AudioContext): AudioNode {
  const gain = context.createGain(),
    limiter = context.createDynamicsCompressor(),
    clip = context.createWaveShaper();
  gain.gain.value = 10 ** (MASTER_GAIN_DB / 20);
  limiter.threshold.value = -4;
  limiter.knee.value = 0;
  limiter.ratio.value = 20;
  limiter.attack.value = 0.001;
  limiter.release.value = 0.1;
  // Unity slope at normal levels; only transients the limiter misses are rounded off.
  clip.curve = Float32Array.from(
    { length: 2049 },
    (_, i) => CLIP_CEILING * Math.tanh((i - 1024) / 1024 / CLIP_CEILING),
  );
  clip.oversample = '4x';
  gain.connect(limiter);
  limiter.connect(clip);
  clip.connect(context.destination);
  return gain;
}

export type LayerName = 'bed' | 'roar' | 'roar_alt' | 'cheer' | 'pocket' | 'groan' | 'boo';
export type StingerName = 'intro' | 'quarter' | 'halftime' | 'final' | 'score';
/** A decoded recording and the part of it the stadium uses. */
export type Region = { buffer: AudioBuffer; start: number; end: number; gain: number };
export type StadiumAssets = {
  layers: Partial<Record<LayerName, Region>>;
  stingers: Partial<Record<StingerName, Region>>;
};
type ManifestEntry = { file: string; start?: number; end?: number; gain_db?: number };

/** Fetch and decode the CC0 crowd layers and designed stingers listed in manifest.json. */
export async function loadStadium(
  context: BaseAudioContext,
  base = '/audio/',
): Promise<StadiumAssets | null> {
  try {
    const response = await fetch(`${base}manifest.json`);
    if (!response.ok) return null;
    const manifest = (await response.json()) as {
      layers?: Record<string, ManifestEntry>;
      stingers?: Record<string, ManifestEntry>;
    };
    const decode = async (entry: ManifestEntry): Promise<Region> => {
      const file = await fetch(base + entry.file);
      if (!file.ok) throw new Error(`Missing ${entry.file}`);
      const buffer = await context.decodeAudioData(await file.arrayBuffer());
      const start = finite(entry.start ?? 0, 0, 0, buffer.duration),
        end = finite(entry.end ?? buffer.duration, buffer.duration, start, buffer.duration);
      return { buffer, start, end, gain: 10 ** (finite(entry.gain_db ?? 0, 0, -40, 20) / 20) };
    };
    const group = async <K extends string>(entries: Record<string, ManifestEntry> = {}) =>
      Object.fromEntries(
        await Promise.all(
          Object.entries(entries).map(async ([name, entry]) => [name, await decode(entry)]),
        ),
      ) as Partial<Record<K, Region>>;
    const layers = await group<LayerName>(manifest.layers),
      stingers = await group<StingerName>(manifest.stingers);
    if (layers.bed) layers.bed = seamless(context, layers.bed);
    return { layers, stingers };
  } catch {
    // Missing or undecodable files leave the procedural crowd in place.
    return null;
  }
}

/** Loop a region without a click: its tail crossfades (equal power) into its head. */
export function seamless(context: BaseAudioContext, region: Region, fade = 1.5): Region {
  const rate = region.buffer.sampleRate,
    first = Math.floor(region.start * rate),
    total = Math.floor((region.end - region.start) * rate),
    overlap = Math.min(Math.floor(fade * rate), Math.floor(total / 3)),
    length = total - overlap;
  if (length <= 0) return region;
  const loop = context.createBuffer(region.buffer.numberOfChannels, length, rate);
  for (let channel = 0; channel < loop.numberOfChannels; channel++) {
    const source = region.buffer.getChannelData(channel),
      samples = new Float32Array(length);
    for (let i = 0; i < length; i++) {
      const head = source[first + i] ?? 0;
      if (i < overlap) {
        const angle = (Math.PI / 2) * (i / overlap);
        samples[i] = head * Math.sin(angle) + (source[first + length + i] ?? 0) * Math.cos(angle);
      } else samples[i] = head;
    }
    loop.copyToChannel(samples, channel);
  }
  return { buffer: loop, start: 0, end: loop.duration, gain: region.gain };
}

type Texture = 'bed' | 'crowd_roar' | 'crowd_gasp' | 'crowd_swell';
// Stingers are production, not crowd: a reaction never fades one out.
type Playing = {
  source: AudioBufferSourceNode;
  gain: GainNode;
  nodes: AudioNode[];
  stinger?: boolean;
};

// Levels on top of layers normalized to -20 LUFS, which is about where the commentators sit.
const BED = 0.4; // about -28 LUFS between lines
const BED_DUCK = 0.316; // -10 dB more under speech
const REACTION_DUCK = 0.63; // reactions only dip -4 dB, so a touchdown call rides on the roar
const STINGER = 0.8;
// Recordings: [layer, peak per unit of intensity, attack s, release s]
const SHAPES: Record<string, [LayerName, number, number, number]> = {
  crowd_roar: ['roar', 1.0, 0.06, 2.2],
  crowd_cheer: ['cheer', 0.8, 0.08, 1.6],
  crowd_swell: ['cheer', 0.6, 0.25, 1.4],
  crowd_groan: ['groan', 0.9, 0.04, 0.5],
  crowd_gasp: ['groan', 0.7, 0.03, 0.4],
  crowd_boo: ['boo', 0.8, 0.3, 1.5],
  crowd_build: ['pocket', 0.55, 3, 1.2],
};

/**
 * The stadium heard from the home side: a looped crowd bed, reactions that swell
 * on cues (roar, cheer, groan, boo, a pre-snap build), a distant pocket of
 * visiting fans, and designed stingers between segments. Recordings are CC0 and
 * listed in public/audio/LICENSES.md; a procedural texture stands in when they
 * cannot load. SpeechSynthesis cannot be routed into Web Audio, so both voice
 * paths explicitly duck the crowd; PCM shares a compressor with it for headroom.
 */
export class BroadcastSoundscape {
  /** Replaceable for tests and offline use. */
  static loader: (context: BaseAudioContext) => Promise<StadiumAssets | null> = loadStadium;

  private enabled = false;
  private active = false;
  private speaking = false;
  private mix: DynamicsCompressorNode | null = null;
  private voice: GainNode | null = null;
  private atmosphere: GainNode | null = null;
  private reactions: GainNode | null = null;
  private stingerBus: GainNode | null = null;
  // The bed's own swell, untouched by ducking: a pre-snap build keeps rising after the line ends.
  private swell: GainNode | null = null;
  private filters: BiquadFilterNode[] = [];
  private bed: Playing | null = null;
  private buffers = new Map<string, AudioBuffer>();
  private cues = new Set<Playing>();
  private heard = new Set<string>();
  private lastCueAt = -Infinity;
  private level = 1;
  private assets: StadiumAssets | null = null;
  private loading: Promise<void> | null = null;
  private roars = 0;

  constructor(
    private context: AudioContext,
    private output: AudioNode = context.destination,
  ) {}

  get voiceDestination(): AudioNode {
    return this.voice ?? this.context.destination;
  }

  /** Recordings decoded and in use (false: procedural fallback). */
  get recorded(): boolean {
    return this.assets !== null;
  }

  /** Resolves once the recordings have loaded or the fallback is settled. */
  ready(): Promise<void> {
    return this.loading ?? Promise.resolve();
  }

  setEnabled(value: boolean) {
    if (this.enabled === value) return;
    this.enabled = value;
    if (!value) this.stop();
    else {
      this.ensureGraph();
      this.syncBed();
    }
  }

  setActive(value: boolean) {
    if (this.active === value) return;
    this.active = value;
    if (!value) this.stop();
    else this.syncBed();
  }

  setSpeaking(value: boolean) {
    if (this.speaking === value) return;
    this.speaking = value;
    this.applyLevels(value ? 0.035 : 0.24);
  }

  setLevel(value: number) {
    // The top of the slider stays reasonable; zero removes the crowd entirely.
    const amount = finite(value, 0.35, 0, 1);
    const level = amount <= 0.35 ? amount / 0.35 : 1 + ((amount - 0.35) / 0.65) * 0.8;
    if (this.level === level) return;
    this.level = level;
    this.applyLevels(0.12);
  }

  cue(eventKey: string, delivery?: VocalDelivery | null, when = this.context.currentTime) {
    if (!eventKey || !delivery || delivery.sound_cue === 'none') return;
    // Mark even disabled events heard: switching sound on never replays history.
    if (this.heard.has(eventKey)) return;
    this.heard.add(eventKey);
    if (this.heard.size > 1024) this.heard.delete(this.heard.values().next().value!);
    if (!this.enabled || !this.active || this.context.state === 'closed') return;
    const intensity = finite(delivery.cue_intensity, 0, 0, 1);
    if (!intensity || !SHAPES[delivery.sound_cue]) return;
    const start = Math.max(this.context.currentTime, when);
    this.ensureGraph();
    if (this.assets) this.react(delivery, intensity, start);
    else this.texturedReaction(delivery, intensity, start);
  }

  /**
   * Play a designed stinger at `when`. Returns how long the voice should wait
   * so it enters on the stinger's tail (0 when there is none to play).
   */
  stinger(kind: VocalDelivery['stinger'], when = this.context.currentTime): number {
    if (!kind || kind === 'none' || !this.enabled || this.context.state === 'closed') return 0;
    const region = this.assets?.stingers[kind];
    if (!region || !this.stingerBus) return 0;
    const playing = this.play(
      region,
      this.stingerBus,
      STINGER,
      Math.max(this.context.currentTime, when),
    );
    if (!playing) return 0;
    playing.stinger = true;
    return Math.max(0, region.end - region.start - 0.9);
  }

  /** The listener's own cheer or boo, heard at once from their section of the stands. */
  listener(kind: 'cheer' | 'boo'): boolean {
    if (!this.enabled || !this.active || this.context.state === 'closed' || !this.reactions)
      return false;
    const region = this.assets?.layers[kind === 'cheer' ? 'pocket' : 'boo'];
    if (!region) return false;
    return (
      this.play(region, this.reactions, kind === 'cheer' ? 0.5 : 0.45, this.context.currentTime, {
        attack: 0.05,
        release: 0.8,
        hold: 2.2,
      }) !== null
    );
  }

  /** Immediate cleanup for mute/pause/disconnect; effect preferences remain. */
  stop() {
    this.active = false;
    for (const bus of [this.atmosphere, this.reactions, this.stingerBus])
      if (bus) this.ramp(bus.gain, 0, 0.01);
    if (this.bed) {
      this.release(this.bed);
      this.bed = null;
    }
    for (const playing of this.cues) this.release(playing);
    this.cues.clear();
    if (this.swell) {
      this.swell.gain.cancelScheduledValues(0);
      this.swell.gain.value = 1;
    }
    this.speaking = false;
  }

  private react(delivery: VocalDelivery, intensity: number, start: number) {
    const [name, peak, attack, release] = SHAPES[delivery.sound_cue];
    let layer = name;
    // Two roar recordings alternate, so a long game never repeats the same one twice running.
    if (layer === 'roar' && delivery.reason !== 'touchdown' && this.roars++ % 2) layer = 'roar_alt';
    const region = this.assets!.layers[layer] ?? this.assets!.layers.cheer;
    if (!region) return;
    // The new reaction takes over: a build gives way to the roar or the groan.
    for (const playing of this.cues) if (!playing.stinger) this.fadeOut(playing, start, 0.3);
    const duration = region.end - region.start;
    const hold = layer === 'pocket' ? Math.min(duration, 8) : duration;
    this.play(region, this.reactions!, peak * intensity, start, { attack, release, hold });
    const pocket = finite(delivery.away_pocket ?? 0, 0, 0, 1);
    const away = this.assets!.layers.pocket;
    if (pocket && away && layer !== 'pocket')
      this.play(away, this.reactions!, 0.45 * pocket, start + 0.15, {
        attack: 0.12,
        release: 1.2,
        hold: Math.min(6, away.end - away.start),
        distant: true,
      });
    if (this.swell) {
      const param = this.swell.gain;
      param.cancelScheduledValues(start);
      if (delivery.sound_cue === 'crowd_build') {
        // The whole building leans in: the bed swells under the rising section.
        param.setTargetAtTime(1 + intensity, start, 1.2);
        param.setTargetAtTime(1, start + hold, 0.8);
      } else param.setTargetAtTime(1, start, 0.3);
    }
  }

  private play(
    region: Region,
    bus: AudioNode,
    level: number,
    start: number,
    shape: { attack?: number; release?: number; hold?: number; distant?: boolean } = {},
  ): Playing | null {
    try {
      const source = this.context.createBufferSource(),
        gain = this.context.createGain(),
        nodes: AudioNode[] = [];
      source.buffer = region.buffer;
      const duration = Math.min(shape.hold ?? Infinity, region.end - region.start),
        attack = Math.min(shape.attack ?? 0.01, duration / 2),
        release = Math.min(shape.release ?? 0.05, duration / 2),
        peak = level * region.gain;
      gain.gain.setValueAtTime(0, start);
      gain.gain.linearRampToValueAtTime(peak, start + attack);
      gain.gain.setValueAtTime(peak, start + duration - release);
      gain.gain.linearRampToValueAtTime(0, start + duration);
      source.connect(gain);
      let tail: AudioNode = gain;
      if (shape.distant) {
        // Visiting fans across the stadium: narrower, further, off to one side.
        const low = this.context.createBiquadFilter(),
          high = this.context.createBiquadFilter(),
          pan = this.context.createStereoPanner();
        low.type = 'highpass';
        low.frequency.value = 250;
        high.type = 'lowpass';
        high.frequency.value = 3200;
        pan.pan.value = -0.65;
        gain.connect(low);
        low.connect(high);
        high.connect(pan);
        nodes.push(low, high, pan);
        tail = pan;
      }
      tail.connect(bus);
      const playing = { source, gain, nodes };
      this.cues.add(playing);
      source.onended = () => {
        this.cues.delete(playing);
        this.disconnect(playing);
      };
      source.start(start, region.start, duration);
      return playing;
    } catch {
      return null;
    }
  }

  private fadeOut(playing: Playing, at: number, time: number) {
    try {
      const param = playing.gain.gain;
      param.cancelScheduledValues(at);
      param.setTargetAtTime(0, at, time / 3);
      playing.source.stop(at + time);
    } catch {
      this.release(playing);
    }
  }

  /** The original quiet procedural texture, used only when recordings are unavailable. */
  private texturedReaction(delivery: VocalDelivery, intensity: number, start: number) {
    const cue = delivery.sound_cue;
    const texture: Texture = ['crowd_roar', 'crowd_cheer'].includes(cue)
      ? 'crowd_roar'
      : ['crowd_groan', 'crowd_gasp', 'crowd_boo'].includes(cue)
        ? 'crowd_gasp'
        : 'crowd_swell';
    // At most one short reaction at a time, including accelerated replay.
    if (start - this.lastCueAt < 3 || this.cues.size) return;
    const source = this.context.createBufferSource(),
      gain = this.context.createGain();
    source.buffer = this.texture(texture);
    const peak =
      (texture === 'crowd_roar' ? 3.4 : texture === 'crowd_gasp' ? 2.2 : 2.6) * intensity * 0.11;
    gain.gain.setValueAtTime(peak, start);
    source.connect(gain);
    gain.connect(this.reactions!);
    const playing = { source, gain, nodes: [] };
    this.cues.add(playing);
    source.onended = () => {
      this.cues.delete(playing);
      this.disconnect(playing);
    };
    source.start(start);
    this.lastCueAt = start;
  }

  private release(playing: Playing) {
    playing.source.onended = null;
    try {
      playing.source.stop();
    } catch {
      /* already ended */
    }
    this.disconnect(playing);
  }

  private disconnect(playing: Playing) {
    for (const node of [playing.source, playing.gain, ...playing.nodes]) {
      try {
        node.disconnect();
      } catch {
        /* already disconnected */
      }
    }
  }

  private ramp(param: AudioParam, value: number, time: number) {
    const now = this.context.currentTime;
    if (typeof param.cancelAndHoldAtTime === 'function') param.cancelAndHoldAtTime(now);
    else {
      param.cancelScheduledValues(now);
      param.setValueAtTime(param.value, now);
    }
    param.setTargetAtTime(value, now, time);
  }

  private bedLevel() {
    // The procedural texture was designed for these quieter values.
    const base = this.assets ? BED : 0.11;
    return this.level * base * (this.speaking ? (this.assets ? BED_DUCK : 0.41) : 1);
  }

  private applyLevels(time: number) {
    const on = this.enabled && this.active;
    if (this.atmosphere) this.ramp(this.atmosphere.gain, on ? this.bedLevel() : 0, time);
    if (this.reactions)
      this.ramp(
        this.reactions.gain,
        on ? this.level * (this.speaking ? REACTION_DUCK : 1) : 0,
        time,
      );
    if (this.stingerBus) this.ramp(this.stingerBus.gain, this.enabled ? 1 : 0, 0.02);
  }

  private ensureGraph() {
    if (this.mix) return;
    const mix = this.context.createDynamicsCompressor(),
      output = this.context.createGain(),
      voice = this.context.createGain(),
      atmosphere = this.context.createGain(),
      reactions = this.context.createGain(),
      stingers = this.context.createGain(),
      swell = this.context.createGain(),
      highpass = this.context.createBiquadFilter(),
      lowpass = this.context.createBiquadFilter();
    mix.threshold.value = -8;
    mix.knee.value = 8;
    mix.ratio.value = 4;
    mix.attack.value = 0.005;
    mix.release.value = 0.22;
    output.gain.value = 0.85;
    voice.gain.value = 0.96;
    atmosphere.gain.value = 0;
    reactions.gain.value = 0;
    stingers.gain.value = 0;
    swell.gain.value = 1;
    swell.connect(atmosphere);
    // The procedural texture needs a narrow band; recordings open it once loaded.
    highpass.type = 'highpass';
    highpass.frequency.value = 160;
    lowpass.type = 'lowpass';
    lowpass.frequency.value = 1800;
    voice.connect(mix);
    atmosphere.connect(highpass);
    reactions.connect(highpass);
    highpass.connect(lowpass);
    lowpass.connect(mix);
    stingers.connect(mix);
    mix.connect(output);
    output.connect(this.output);
    this.mix = mix;
    this.voice = voice;
    this.atmosphere = atmosphere;
    this.reactions = reactions;
    this.stingerBus = stingers;
    this.swell = swell;
    this.filters = [highpass, lowpass];
    this.loading = BroadcastSoundscape.loader(this.context)
      .then((assets) => {
        if (!assets?.layers.bed || this.context.state === 'closed') return;
        this.assets = assets;
        this.filters[0].frequency.value = 90;
        this.filters[1].frequency.value = 9000;
        // Swap the placeholder texture for the recorded crowd without a gap.
        if (this.bed) {
          this.release(this.bed);
          this.bed = null;
        }
        this.syncBed();
      })
      .catch(() => {
        /* the procedural crowd remains */
      });
  }

  private syncBed() {
    if (!this.enabled || !this.active || this.context.state === 'closed') return;
    this.ensureGraph();
    if (!this.bed) {
      const region = this.assets?.layers.bed;
      const source = this.context.createBufferSource(),
        gain = this.context.createGain();
      source.buffer = region?.buffer ?? this.texture('bed');
      source.loop = true;
      gain.gain.value = region?.gain ?? 1;
      source.connect(gain);
      gain.connect(this.swell ?? this.atmosphere!);
      // Start somewhere new in the loop each time, so pauses do not restart the same cheer.
      const offset = region ? Math.random() * Math.max(0, region.buffer.duration - 1) : 0;
      source.start(this.context.currentTime, offset);
      this.bed = { source, gain, nodes: [] };
    }
    this.applyLevels(this.speaking ? 0.035 : 0.18);
  }

  private texture(kind: Texture): AudioBuffer {
    const cached = this.buffers.get(kind);
    if (cached) return cached;
    const rate = 24000,
      duration =
        kind === 'bed' ? 8 : kind === 'crowd_roar' ? 2.8 : kind === 'crowd_gasp' ? 0.9 : 1.7,
      length = Math.ceil(duration * rate),
      buffer = this.context.createBuffer(2, length, rate);
    const vowel = Float32Array.from({ length: 1024 }, (_, index) => {
      const phase = (2 * Math.PI * index) / 1024;
      return Math.sin(phase) + 0.32 * Math.sin(phase * 2) + 0.12 * Math.sin(phase * 3);
    });
    for (let channel = 0; channel < 2; channel++) {
      const samples = new Float32Array(length);
      const voices = Array.from({ length: 8 }, (_, voice) => ({
        frequency: 126 + voice * 24.7 + channel * 5.9,
        offset: voice * 0.89 + channel * 1.37,
        phase: (voice * 0.137 + channel * 0.211) % 1,
        step: 0,
        amplitude: 0,
      }));
      let seed = 18731 + channel * 127 + kind.length * 13,
        smooth = 0,
        slow = 0;
      for (let i = 0; i < length; i++) {
        seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
        const noise = (seed / 0xffffffff) * 2 - 1,
          t = i / rate;
        smooth = 0.84 * smooth + 0.16 * noise;
        slow = 0.985 * slow + 0.015 * noise;
        // Speech-like bands and slow collective movement, without distinct words.
        const movement = 0.72 + 0.13 * Math.sin(t * 2.8 + channel) + 0.1 * Math.sin(t * 5.7);
        let babble = 0;
        for (let voice = 0; voice < voices.length; voice++) {
          const singer = voices[voice];
          if (i % 128 === 0) {
            singer.step = (singer.frequency + 4.7 * Math.cos(t * 2.1 + singer.offset)) / rate;
            singer.amplitude =
              Math.pow(Math.max(0, Math.sin(t * (1.7 + voice * 0.23) + singer.offset)), 3) * 0.009;
          }
          // Soft, staggered vowel-like fragments create distant babble,
          // without a repeated whistle or an intelligible extra speaker.
          babble += vowel[Math.floor(singer.phase * vowel.length)] * singer.amplitude;
          singer.phase += singer.step;
          if (singer.phase >= 1) singer.phase -= 1;
        }
        const texture = ((smooth - slow) * 0.53 + slow * 1.15 + babble) * movement;
        if (kind === 'bed') {
          // Match loop edges with silence to avoid periodic clicks.
          const edge = Math.min(1, t / 0.25, (duration - t) / 0.25);
          samples[i] = texture * 0.55 * Math.max(0, edge);
        } else {
          const attack = kind === 'crowd_gasp' ? 0.08 : 0.18;
          const envelope = Math.min(1, t / attack) * Math.pow(Math.max(0, 1 - t / duration), 0.65);
          const cheer = Math.sin(2 * Math.PI * (390 * t + 18 * Math.sin(t * 3.1))) * 0.015;
          samples[i] = (texture + cheer) * envelope;
        }
      }
      buffer.copyToChannel(samples, channel);
    }
    this.buffers.set(kind, buffer);
    return buffer;
  }
}
