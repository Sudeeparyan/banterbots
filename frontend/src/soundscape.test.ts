import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  BroadcastSoundscape,
  browserDelivery,
  createMaster,
  loadStadium,
  seamless,
  speechPhrases,
  type Region,
  type StadiumAssets,
} from './soundscape';
import type { VocalDelivery } from './types';

const plan = (overrides: Partial<VocalDelivery> = {}): VocalDelivery => ({
  energy: 0.8,
  pace: 'brisk',
  contour: 'lift_and_settle',
  browser_rate: 1.08,
  browser_pitch: 1.07,
  browser_volume: 0.97,
  pause_before_ms: 180,
  sound_cue: 'crowd_roar',
  cue_intensity: 0.9,
  reason: 'touchdown',
  ...overrides,
});

class Param {
  value = 1;
  cancelScheduledValues = vi.fn();
  cancelAndHoldAtTime = vi.fn();
  setValueAtTime = vi.fn((value: number) => {
    this.value = value;
  });
  linearRampToValueAtTime = vi.fn((value: number) => {
    this.value = value;
  });
  setTargetAtTime = vi.fn((value: number) => {
    this.value = value;
  });
}
class Node {
  connect = vi.fn();
  disconnect = vi.fn();
}
class Gain extends Node {
  gain = new Param();
}
class Source extends Node {
  buffer: Buffer | null = null;
  loop = false;
  onended: (() => void) | null = null;
  start = vi.fn();
  stop = vi.fn();
}
class Buffer {
  channels: Float32Array[] = [];
  duration: number;
  constructor(
    public numberOfChannels: number,
    public length: number,
    public sampleRate: number,
  ) {
    this.duration = length / sampleRate;
    this.channels = Array.from({ length: numberOfChannels }, () => new Float32Array(length));
  }
  copyToChannel = (samples: Float32Array, channel: number) => {
    this.channels[channel] = samples;
  };
  getChannelData = (channel: number) => this.channels[channel];
}
class Context {
  currentTime = 10;
  state = 'running';
  destination = new Node();
  sources: Source[] = [];
  gains: Gain[] = [];
  panners: (Node & { pan: Param })[] = [];
  compressors: (Node & {
    threshold: Param;
    knee: Param;
    ratio: Param;
    attack: Param;
    release: Param;
  })[] = [];
  createGain() {
    const gain = new Gain();
    this.gains.push(gain);
    return gain;
  }
  createBufferSource() {
    const source = new Source();
    this.sources.push(source);
    return source;
  }
  createBuffer(channels: number, length: number, rate: number) {
    return new Buffer(channels, length, rate);
  }
  createBiquadFilter() {
    return Object.assign(new Node(), { frequency: new Param(), type: '' });
  }
  createStereoPanner() {
    const panner = Object.assign(new Node(), { pan: new Param() });
    this.panners.push(panner);
    return panner;
  }
  createDynamicsCompressor() {
    const compressor = Object.assign(new Node(), {
      threshold: new Param(),
      knee: new Param(),
      ratio: new Param(),
      attack: new Param(),
      release: new Param(),
    });
    this.compressors.push(compressor);
    return compressor;
  }
}
// Gains in creation order: output, voice, bed bus, reaction bus, stinger bus, swell.
const VOICE = 1,
  BED = 2,
  REACTIONS = 3,
  SWELL = 5;
const region = (seconds: number, gain = 0.5): Region => ({
  buffer: new Buffer(2, seconds * 1000, 1000) as unknown as AudioBuffer,
  start: 0,
  end: seconds,
  gain,
});
const recorded = (): StadiumAssets => ({
  layers: {
    bed: region(20),
    roar: region(10),
    roar_alt: region(7),
    cheer: region(9),
    pocket: region(9),
    groan: region(2),
    boo: region(11),
  },
  stingers: { intro: region(2.7), final: region(3) },
});
const setup = async (assets: StadiumAssets | null = recorded()) => {
  BroadcastSoundscape.loader = async () => assets;
  const context = new Context(),
    scene = new BroadcastSoundscape(context as unknown as AudioContext);
  scene.setEnabled(true);
  scene.setActive(true);
  await scene.ready();
  return { context, scene };
};
const played = (context: Context, from = 0) =>
  context.sources.slice(from).map((source) => source.buffer?.duration);
afterEach(() => {
  BroadcastSoundscape.loader = loadStadium;
});

describe('expressive browser speech', () => {
  it('keeps player initials, name abbreviations, decimals and scores intact', () => {
    const text =
      'A.J. Brown takes 12.5 yards for K.C.! St. Brown answers with six. The score is 14–7.';
    const phrases = speechPhrases(text, plan());
    expect(phrases).toEqual([
      'A.J. Brown takes 12.5 yards for K.C.!',
      'St. Brown answers with six.',
      'The score is 14–7.',
    ]);
    expect(phrases.join(' ')).toBe(text);
    expect(speechPhrases('J.J. McCarthy finds Dr. Brown. That is the play.', plan())).toEqual([
      'J.J. McCarthy finds Dr. Brown.',
      'That is the play.',
    ]);
  });

  it('bounds untrusted delivery values and settles excitement after the reaction', () => {
    const first = browserDelivery('a', plan(), 0),
      second = browserDelivery('a', plan(), 1);
    expect(first.pitch).toBeGreaterThan(second.pitch);
    expect(first.rate).toBeLessThan(second.rate);
    const bounded = browserDelivery(
      'b',
      plan({
        browser_rate: Infinity,
        browser_pitch: 20,
        browser_volume: -2,
        pause_before_ms: 10000,
      }),
    );
    expect(bounded.rate).toBeGreaterThanOrEqual(0.85);
    expect(bounded.rate).toBeLessThanOrEqual(1.2);
    expect(bounded.pitch).toBe(1.2);
    expect(bounded.volume).toBe(0.7);
    expect(bounded.pause).toBe(600);
    const shocked = plan({ contour: 'stunned_drop' });
    expect(browserDelivery('b', shocked, 0).pitch).toBeGreaterThan(
      browserDelivery('b', shocked, 1).pitch,
    );
    const explode = browserDelivery('a', plan({ contour: 'explode' }), 0);
    expect(explode.pitch).toBeGreaterThan(first.pitch);
  });
});

describe('recorded stadium', () => {
  it('loops the recorded bed, then stops every source on pause and ignores cues while paused', async () => {
    const { context, scene } = await setup();
    expect(scene.recorded).toBe(true);
    // The placeholder texture plays until the recordings decode, then hands over.
    const [placeholder, bed] = context.sources.filter((source) => source.loop);
    expect(placeholder.stop).toHaveBeenCalled();
    expect(bed.buffer!.duration).toBe(20);
    scene.cue('session:1:td:0', plan());
    const reaction = context.sources[context.sources.length - 1];
    scene.setActive(false);
    expect(bed.stop).toHaveBeenCalled();
    expect(reaction.stop).toHaveBeenCalled();
    expect(reaction.disconnect).toHaveBeenCalled();
    const count = context.sources.length;
    scene.cue('session:1:paused:0', plan());
    expect(context.sources).toHaveLength(count);
  });

  it('ducks the bed 10 dB under a voice but a reaction only 4 dB, and the level moves only the crowd', async () => {
    const { context, scene } = await setup();
    const bed = context.gains[BED].gain,
      reactions = context.gains[REACTIONS].gain,
      voice = context.gains[VOICE],
      voiceGain = voice.gain.value;
    expect(bed.value).toBeCloseTo(0.4);
    scene.setSpeaking(true);
    expect(bed.value).toBeCloseTo(0.4 * 0.316);
    expect(reactions.value).toBeCloseTo(0.63);
    scene.setSpeaking(false);
    expect(bed.setTargetAtTime).toHaveBeenLastCalledWith(0.4, 10, 0.24);
    scene.setLevel(0);
    expect(bed.value).toBe(0);
    expect(reactions.value).toBe(0);
    scene.setLevel(1);
    expect(bed.value).toBeCloseTo(0.72);
    expect(voice.gain.value).toBe(voiceGain);
    expect(scene.voiceDestination).toBe(voice);
    expect(context.compressors[0].ratio.value).toBeGreaterThan(1);
  });

  it('roars for a touchdown, groans with a pocket of away fans, boos a flag and builds before third down', async () => {
    const { context, scene } = await setup();
    let from = context.sources.length;
    scene.cue('s:1:td:0', plan(), 10.25);
    expect(played(context, from)).toEqual([10]);
    expect(context.sources[from].start).toHaveBeenCalledWith(10.25, 0, 10);
    from = context.sources.length;
    scene.cue('s:1:rival-td:0', plan({ sound_cue: 'crowd_groan', away_pocket: 0.7 }), 12);
    // The home groan, then the visitors' distant cheer off to one side.
    expect(played(context, from)).toEqual([2, 9]);
    expect(context.sources[from + 1].start).toHaveBeenCalledWith(12.15, 0, 6);
    expect(context.panners).toHaveLength(1);
    from = context.sources.length;
    scene.cue('s:1:flag:0', plan({ sound_cue: 'crowd_boo', cue_intensity: 0.55 }), 20);
    expect(played(context, from)).toEqual([11]);
    from = context.sources.length;
    scene.cue('s:1:third-down:0', plan({ sound_cue: 'crowd_build', cue_intensity: 0.8 }), 30);
    expect(played(context, from)).toEqual([9]);
    const swell = context.gains[SWELL].gain;
    expect(swell.setTargetAtTime).toHaveBeenCalledWith(1.8, 30, 1.2);
    // The snap's result takes over from the build.
    const build = context.sources[from];
    scene.cue('s:1:conversion:0', plan({ sound_cue: 'crowd_cheer', cue_intensity: 0.55 }), 34);
    expect(build.stop).toHaveBeenCalledWith(34.3);
    expect(swell.setTargetAtTime).toHaveBeenLastCalledWith(1, 34, 0.3);
  });

  it('plays a cue once per shared event and never replays one heard while sound was off', async () => {
    BroadcastSoundscape.loader = async () => recorded();
    const context = new Context(),
      scene = new BroadcastSoundscape(context as unknown as AudioContext);
    scene.cue('session:1:play:0', plan());
    scene.setEnabled(true);
    scene.setActive(true);
    await scene.ready();
    const count = context.sources.length;
    scene.cue('session:1:play:0', plan());
    scene.cue('session:1:routine:0', plan({ sound_cue: 'none' }));
    expect(context.sources).toHaveLength(count);
    scene.cue('session:2:play:0', plan());
    expect(context.sources).toHaveLength(count + 1);
    scene.stop();
  });

  it('opens segments with a designed stinger and tells the voice when to come in', async () => {
    const { context, scene } = await setup();
    const from = context.sources.length;
    const lead = scene.stinger('intro');
    expect(lead).toBeCloseTo(1.8);
    expect(played(context, from)).toEqual([2.7]);
    // A crowd reaction during the stinger never cuts it short.
    scene.cue('s:1:intro-roar:0', plan());
    expect(context.sources[from].stop).not.toHaveBeenCalled();
    expect(scene.stinger('quarter')).toBe(0); // not in this set: no wait, no sound
    expect(scene.stinger('none')).toBe(0);
    scene.setEnabled(false);
    expect(scene.stinger('final')).toBe(0);
  });
});

describe('stadium assets', () => {
  it('crossfades the bed loop so its seam has no jump', () => {
    const context = new Context(),
      source = new Buffer(1, 4000, 1000);
    source.channels[0] = Float32Array.from({ length: 4000 }, (_, i) => Math.sin(i / 37));
    const loop = seamless(
      context as unknown as BaseAudioContext,
      { buffer: source as unknown as AudioBuffer, start: 0.5, end: 3.5, gain: 0.3 },
      0.5,
    );
    const samples = (loop.buffer as unknown as Buffer).channels[0];
    expect(loop.buffer.duration).toBeCloseTo(2.5);
    expect(loop.gain).toBe(0.3);
    // The last sample flows into the first, as in the original recording.
    expect(Math.abs(samples[samples.length - 1] - samples[0])).toBeLessThan(0.05);
  });

  it('decodes the manifest, trims and applies loudness gain, and falls back when a file is missing', async () => {
    const decoded = new Buffer(2, 30000, 1000);
    const context = {
      decodeAudioData: vi.fn(async () => decoded),
      createBuffer: new Context().createBuffer,
    };
    const manifest = {
      layers: { bed: { file: 'crowd/bed.mp3', start: 3, end: 28, gain_db: -6 } },
      stingers: { intro: { file: 'stingers/intro.wav', gain_db: 0 } },
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => ({
        ok: true,
        json: async () => manifest,
        arrayBuffer: async () => new ArrayBuffer(8),
        url,
      })),
    );
    const assets = await loadStadium(context as unknown as BaseAudioContext);
    expect(assets?.stingers.intro?.gain).toBeCloseTo(1);
    expect(assets?.layers.bed?.gain).toBeCloseTo(0.501, 2);
    expect(assets?.layers.bed?.buffer.duration).toBeCloseTo(23.5); // 25 s region less the crossfade
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => ({ ok: !url.endsWith('.mp3'), json: async () => manifest })),
    );
    expect(await loadStadium(context as unknown as BaseAudioContext)).toBeNull();
    vi.unstubAllGlobals();
  });
});

describe('procedural fallback', () => {
  it('keeps the quiet texture, one reaction at a time, when recordings are unavailable', async () => {
    const { context, scene } = await setup(null);
    expect(scene.recorded).toBe(false);
    expect(context.sources).toHaveLength(1);
    scene.setSpeaking(true);
    expect(context.gains[BED].gain.value).toBeCloseTo(0.045, 3);
    scene.cue('session:1:play:0', plan(), 10.25);
    expect(context.sources[1].start).toHaveBeenCalledExactlyOnceWith(10.25);
    scene.cue('session:1:peer:0', plan(), 11);
    expect(context.sources).toHaveLength(2);
    expect(scene.stinger('intro')).toBe(0);
    scene.stop();
    expect(context.sources[0].stop).toHaveBeenCalled();
  });

  it('generates finite stereo textures with clean edges and no external samples', async () => {
    const { context, scene } = await setup(null);
    scene.cue('event', plan());
    const bed = context.sources[0].buffer!,
      cue = context.sources[1].buffer!;
    expect(cue.duration).toBeLessThan(3);
    for (const buffer of [bed, cue]) {
      expect(buffer.channels).toHaveLength(2);
      for (const samples of buffer.channels) {
        let squares = 0,
          peak = 0,
          allFinite = true;
        for (const sample of samples) {
          allFinite &&= Number.isFinite(sample);
          squares += sample * sample;
          peak = Math.max(peak, Math.abs(sample));
        }
        expect(allFinite).toBe(true);
        expect(peak).toBeLessThan(0.6);
        expect(Math.sqrt(squares / samples.length)).toBeGreaterThan(0.01);
        expect(Math.abs(samples[0])).toBe(0);
        expect(Math.abs(samples[samples.length - 1])).toBeLessThan(0.001);
      }
    }
    expect(bed.channels[0]).not.toEqual(bed.channels[1]);
    scene.stop();
  });
});

describe('master', () => {
  it('lifts the mix with make-up gain, limits peaks and soft-clips under -1 dBFS at unity slope', () => {
    const context = Object.assign(new Context(), {
      createWaveShaper: () =>
        Object.assign(new Node(), { curve: null as Float32Array | null, oversample: '' }),
    });
    const input = createMaster(context as unknown as AudioContext) as unknown as Gain;
    expect(input.gain.value).toBeCloseTo(10 ** (4 / 20));
    const limiter = context.compressors[0];
    expect(limiter.ratio.value).toBe(20);
    const clip = limiter.connect.mock.calls[0][0] as { curve: Float32Array; oversample: string };
    expect(clip.oversample).toBe('4x');
    expect(Math.max(...clip.curve.map(Math.abs))).toBeLessThan(10 ** (-1 / 20));
    // Quiet speech passes unchanged.
    expect(clip.curve[1024 + 102]).toBeCloseTo(102 / 1024, 2);
  });
});
