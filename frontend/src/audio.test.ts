import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BroadcastAudio, decodePCM } from './audio';
import { BroadcastSoundscape } from './soundscape';
import type { VocalDelivery } from './types';

const delivery = (overrides: Partial<VocalDelivery> = {}): VocalDelivery => ({
  energy: 0.85,
  pace: 'brisk',
  contour: 'lift_and_settle',
  browser_rate: 1.07,
  browser_pitch: 1.06,
  browser_volume: 0.96,
  pause_before_ms: 160,
  sound_cue: 'crowd_roar',
  cue_intensity: 0.85,
  reason: 'touchdown',
  ...overrides,
});
const mockScene = () => {
  for (const method of ['setEnabled', 'setActive', 'setSpeaking', 'setLevel', 'stop'] as const)
    vi.spyOn(BroadcastSoundscape.prototype, method).mockImplementation(() => {});
  return vi.spyOn(BroadcastSoundscape.prototype, 'cue').mockImplementation(() => {});
};
const pcm = (samples: number[]) =>
  btoa(String.fromCharCode(...samples.flatMap((v) => [v & 255, (v >> 8) & 255])));
const scheduled: number[] = [];
const stopped: ReturnType<typeof vi.fn>[] = [];
const contexts: Context[] = [];
class Context {
  private origin = Date.now();
  frozen: number | null = null;
  destination = {};
  constructor() {
    contexts.push(this);
  }
  get currentTime() {
    return this.frozen ?? (Date.now() - this.origin) / 1000;
  }
  async resume() {}
  createBuffer(_channels: number, length: number, rate: number) {
    return { duration: length / rate, copyToChannel: vi.fn() };
  }
  createBufferSource() {
    const stop = vi.fn();
    stopped.push(stop);
    return {
      buffer: null as { duration: number } | null,
      connect: vi.fn(),
      stop,
      start: (when: number) => scheduled.push(when),
    };
  }
}
class Utterance {
  voice: unknown = null;
  lang = '';
  rate = 1;
  pitch = 1;
  volume = 1;
  onstart: (() => void) | null = null;
  onend: (() => void) | null = null;
  onerror: ((event?: { error: string }) => void) | null = null;
  constructor(public text: string) {}
}
beforeEach(() => {
  vi.useFakeTimers();
  scheduled.length = 0;
  stopped.length = 0;
  contexts.length = 0;
  vi.stubGlobal('AudioContext', Context);
  vi.stubGlobal('SpeechSynthesisUtterance', Utterance);
  vi.stubGlobal('window', {
    speechSynthesis: { cancel: vi.fn(), getVoices: () => [], speak: vi.fn() },
  });
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
describe('broadcast audio ownership', () => {
  it('holds expressive phrases until the complete line finishes, then releases its peer', async () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    await audio.unlock();
    audio.demo('Touchdown! That drive came together.', 'a', 'lead', delivery());
    audio.demo('The protection made that possible.', 'b', 'peer');
    const speak = vi.mocked(window.speechSynthesis.speak);
    expect(speak).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(160);
    const first = speak.mock.calls[0][0] as unknown as Utterance;
    expect(first.text).toBe('Touchdown!');
    first.onstart?.();
    first.onend?.();
    first.onend?.();
    expect(ack).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(100);
    const second = speak.mock.calls[1][0] as unknown as Utterance;
    expect(second.text).toBe('That drive came together.');
    expect(second.pitch).toBeLessThan(first.pitch);
    expect(second.rate).toBeGreaterThan(first.rate);
    expect(second.volume).toBe(0.96);
    first.onerror?.({ error: 'audio-hardware' });
    second.onstart?.();
    second.onend?.();
    expect(ack).toHaveBeenCalledExactlyOnceWith('lead', 0, false);
    const peer = speak.mock.calls[2][0] as unknown as Utterance;
    expect(peer.text).toBe('The protection made that possible.');
    peer.onend?.();
    expect(ack).toHaveBeenLastCalledWith('peer', 0, false);
  });

  it.each(['before', 'between'])(
    'mute during a %s phrase gap cancels both hosts without restarting',
    async (gap) => {
      const ack = vi.fn(),
        audio = new BroadcastAudio(vi.fn(), ack);
      await audio.unlock();
      audio.demo('Touchdown! A decisive finish.', 'a', 'lead', delivery());
      audio.demo('A reply.', 'b', 'peer');
      const speak = vi.mocked(window.speechSynthesis.speak);
      if (gap === 'between') {
        await vi.advanceTimersByTimeAsync(160);
        const first = speak.mock.calls[0][0] as unknown as Utterance;
        first.onstart?.();
        first.onend?.();
      }
      audio.mute(true);
      await vi.runAllTimersAsync();
      expect(speak).toHaveBeenCalledTimes(gap === 'between' ? 1 : 0);
      expect(ack.mock.calls).toEqual([
        ['lead', 0, true],
        ['peer', 0, true],
      ]);
    },
  );

  it('starts a browser reaction only when speech starts and preserves the shared prepared event key', async () => {
    const cue = mockScene(),
      audio = new BroadcastAudio(vi.fn(), vi.fn()),
      plan = delivery({ pause_before_ms: 0 });
    audio.setSoundscape(true);
    await audio.unlock();
    audio.prepare('lead', 'session:epoch:play:revision', plan);
    audio.demo('Touchdown! A brilliant finish.', 'a', 'lead', plan);
    expect(cue).not.toHaveBeenCalled();
    const speak = vi.mocked(window.speechSynthesis.speak),
      first = speak.mock.calls[0][0] as unknown as Utterance;
    first.onstart?.();
    expect(cue).toHaveBeenCalledExactlyOnceWith('session:epoch:play:revision', plan, undefined);
    first.onend?.();
    await vi.advanceTimersByTimeAsync(100);
    const second = speak.mock.calls[1][0] as unknown as Utterance;
    second.onstart?.();
    expect(cue).toHaveBeenCalledOnce();
    second.onend?.();
    audio.clear();
  });

  it('waits for voiced PCM after silent startup and places the cue at its audible sample', async () => {
    const cue = mockScene(),
      ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack),
      plan = delivery();
    audio.setSoundscape(true);
    await audio.unlock();
    audio.prepare('lead', 'event:1', plan);
    audio.chunk({
      segment_id: 'lead',
      agent_id: 'a',
      chunk_index: 0,
      audio: pcm([0, 0, 0, 0]),
      rate: 8000,
    });
    expect(cue).not.toHaveBeenCalled();
    audio.chunk({
      segment_id: 'lead',
      agent_id: 'a',
      chunk_index: 1,
      audio: pcm([0, 0, 1000, 1000]),
      rate: 8000,
    });
    expect(cue).toHaveBeenCalledOnce();
    expect(cue.mock.calls[0][2]).toBeCloseTo(0.16075);
    audio.seal({ segment_id: 'lead', total_samples: 8 });
    await vi.runAllTimersAsync();
    expect(ack).toHaveBeenCalledExactlyOnceWith('lead', 8, false);
  });

  it('measures PCM start after optional sound generation so its acknowledgement cannot release a peer early', async () => {
    mockScene();
    vi.mocked(BroadcastSoundscape.prototype.setActive).mockImplementation((active) => {
      if (active) vi.setSystemTime(Date.now() + 300);
    });
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    audio.setSoundscape(true);
    await audio.unlock();
    audio.prepare('lead', 'event:1', delivery());
    audio.chunk({
      segment_id: 'lead',
      agent_id: 'a',
      chunk_index: 0,
      audio: pcm([1000, 1000]),
      rate: 8000,
    });
    expect(scheduled[0]).toBeCloseTo(0.46);
    audio.seal({ segment_id: 'lead', total_samples: 2 });
    // The current clock remains before the actual source start despite synthesis work.
    expect(ack).not.toHaveBeenCalled();
    audio.clear();
    expect(ack).toHaveBeenCalledExactlyOnceWith('lead', 0, true);
  });

  it('keeps an idle voice preview free of stadium effects', async () => {
    const cue = mockScene(),
      audio = new BroadcastAudio(vi.fn(), vi.fn());
    audio.setSoundscape(true);
    await audio.testVoice();
    const preview = vi.mocked(window.speechSynthesis.speak).mock
      .calls[0][0] as unknown as Utterance;
    preview.onstart?.();
    expect(BroadcastSoundscape.prototype.setActive).not.toHaveBeenCalledWith(true);
    expect(cue).not.toHaveBeenCalled();
    preview.onend?.();
  });

  it('contains optional soundscape failures while preserving voice playback and cleanup', async () => {
    const ack = vi.fn(),
      issue = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack, issue);
    audio.setSoundscape(true);
    await audio.unlock();
    expect(issue).toHaveBeenCalledWith(
      expect.stringContaining('Stadium sounds are unavailable'),
      false,
    );
    audio.demo('The voice still works.', 'a', 'lead');
    const line = vi.mocked(window.speechSynthesis.speak).mock.calls[0][0] as unknown as Utterance;
    line.onstart?.();
    line.onend?.();
    expect(ack).toHaveBeenCalledExactlyOnceWith('lead', 0, false);
    expect(() => audio.clear()).not.toThrow();
  });

  it('decodes signed little endian PCM16', () => {
    expect([...decodePCM(pcm([0, 32767, -32768]))]).toEqual([0, 32767 / 32768, -1]);
  });
  it('plays chunks in index order and acknowledges after the final sample', async () => {
    const ack = vi.fn(),
      speaking = vi.fn(),
      audio = new BroadcastAudio(speaking, ack);
    await audio.unlock();
    audio.chunk({ segment_id: 'one', agent_id: 'a', chunk_index: 1, audio: pcm([1, 1]) });
    expect(scheduled).toHaveLength(0);
    audio.chunk({ segment_id: 'one', agent_id: 'a', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'one', total_samples: 4 });
    expect(scheduled).toHaveLength(2);
    expect(scheduled[1]).toBeGreaterThan(scheduled[0]);
    expect(ack).not.toHaveBeenCalled();
    await vi.runAllTimersAsync();
    expect(ack).toHaveBeenCalledWith('one', 4, false);
  });
  it('buffers a peer until the lead has finished', async () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    await audio.unlock();
    audio.chunk({ segment_id: 'lead', agent_id: 'a', chunk_index: 0, audio: pcm([1, 1]) });
    audio.chunk({ segment_id: 'peer', agent_id: 'b', chunk_index: 0, audio: pcm([1, 1]) });
    expect(scheduled).toHaveLength(1);
    audio.seal({ segment_id: 'lead', total_samples: 2 });
    audio.seal({ segment_id: 'peer', total_samples: 2 });
    await vi.runAllTimersAsync();
    expect(scheduled).toHaveLength(2);
    expect(ack.mock.calls.map((call) => call[0])).toEqual(['lead', 'peer']);
  });
  it('muting cancels pending audio and never completes it later', async () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    await audio.unlock();
    audio.chunk({ segment_id: 'lead', agent_id: 'a', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'lead', total_samples: 2 });
    audio.mute(true);
    await vi.runAllTimersAsync();
    expect(ack).toHaveBeenCalledTimes(1);
    expect(ack).toHaveBeenCalledWith('lead', 0, true);
  });
  it('muted browser speech is immediately acknowledged without autoplay', () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    audio.demo('A real play.', 'a', 'demo');
    expect(ack).toHaveBeenCalledWith('demo', 0, true);
    expect(window.speechSynthesis.speak).not.toHaveBeenCalled();
  });
  it('discards failed lead audio and safely starts its buffered peer', async () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    await audio.unlock();
    audio.chunk({ segment_id: 'failed', agent_id: 'a', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'failed', total_samples: 2 });
    audio.chunk({ segment_id: 'peer', agent_id: 'b', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'peer', total_samples: 2 });
    audio.seal({ segment_id: 'failed', total_samples: 2, discard_audio: true });
    expect(stopped[0]).toHaveBeenCalledTimes(1);
    expect(stopped[1]).not.toHaveBeenCalled();
    expect(scheduled).toHaveLength(2);
    await vi.runAllTimersAsync();
    expect(ack.mock.calls).toEqual([
      ['failed', 0, true],
      ['peer', 2, false],
    ]);
    audio.chunk({ segment_id: 'failed', agent_id: 'a', chunk_index: 1, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'failed', total_samples: 4 });
    expect(scheduled).toHaveLength(2);
    expect(ack).toHaveBeenCalledTimes(2);
  });
  it('discards a buffered peer without stopping the current speaker', async () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    await audio.unlock();
    audio.chunk({ segment_id: 'lead', agent_id: 'a', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'lead', total_samples: 2 });
    audio.chunk({ segment_id: 'failed-peer', agent_id: 'b', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'failed-peer', total_samples: 2, discard_audio: true });
    expect(stopped[0]).not.toHaveBeenCalled();
    expect(scheduled).toHaveLength(1);
    await vi.runAllTimersAsync();
    expect(ack.mock.calls).toEqual([
      ['failed-peer', 0, true],
      ['lead', 2, false],
    ]);
  });
  it('acknowledges a muted failed segment and rejects its late chunks after unmute', async () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    audio.chunk({ segment_id: 'failed', agent_id: 'a', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'failed', total_samples: 2, discard_audio: true });
    await audio.unlock();
    audio.chunk({ segment_id: 'failed', agent_id: 'a', chunk_index: 1, audio: pcm([1, 1]) });
    expect(scheduled).toHaveLength(0);
    expect(ack).toHaveBeenCalledExactlyOnceWith('failed', 0, true);
  });
  it('does not let unmuting halfway through a segment block the next speaker', async () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    audio.chunk({ segment_id: 'muted-lead', agent_id: 'a', chunk_index: 0, audio: pcm([1, 1]) });
    await audio.unlock();
    audio.chunk({ segment_id: 'muted-lead', agent_id: 'a', chunk_index: 1, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'muted-lead', total_samples: 4 });
    audio.chunk({ segment_id: 'peer', agent_id: 'b', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'peer', total_samples: 2 });
    await vi.runAllTimersAsync();
    expect(scheduled).toHaveLength(1);
    expect(ack.mock.calls).toEqual([
      ['muted-lead', 0, true],
      ['peer', 2, false],
    ]);
  });
  it('cannot resurrect a cancelled segment with late chunks or seals', async () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    await audio.unlock();
    audio.chunk({ segment_id: 'cancelled', agent_id: 'a', chunk_index: 0, audio: pcm([1, 1]) });
    audio.clear();
    audio.chunk({ segment_id: 'cancelled', agent_id: 'a', chunk_index: 1, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'cancelled', total_samples: 4 });
    await vi.runAllTimersAsync();
    expect(scheduled).toHaveLength(1);
    expect(ack).toHaveBeenCalledExactlyOnceWith('cancelled', 0, true);
  });
  it('cannot replay a completed segment or acknowledge its duplicate seal twice', async () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    await audio.unlock();
    audio.chunk({ segment_id: 'done', agent_id: 'a', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'done', total_samples: 2 });
    await vi.runAllTimersAsync();
    audio.chunk({ segment_id: 'done', agent_id: 'a', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'done', total_samples: 2 });
    await vi.runAllTimersAsync();
    expect(scheduled).toHaveLength(1);
    expect(ack).toHaveBeenCalledExactlyOnceWith('done', 2, false);
  });
  it('reports only samples actually heard when a playing segment is cancelled', async () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    await audio.unlock();
    audio.chunk({
      segment_id: 'partial',
      agent_id: 'a',
      chunk_index: 0,
      audio: pcm(Array(24000).fill(1)),
    });
    await vi.advanceTimersByTimeAsync(515);
    audio.clear();
    expect(ack).toHaveBeenCalledExactlyOnceWith('partial', 12000, true);
  });
  it('waits for the audio clock when playback is suspended', async () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    await audio.unlock();
    audio.chunk({ segment_id: 'lead', agent_id: 'a', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'lead', total_samples: 2 });
    audio.chunk({ segment_id: 'peer', agent_id: 'b', chunk_index: 0, audio: pcm([1, 1]) });
    contexts[0].frozen = 0;
    await vi.advanceTimersByTimeAsync(500);
    expect(ack).not.toHaveBeenCalled();
    expect(scheduled).toHaveLength(1);
    contexts[0].frozen = null;
    await vi.advanceTimersByTimeAsync(50);
    expect(ack).toHaveBeenCalledExactlyOnceWith('lead', 2, false);
    expect(scheduled).toHaveLength(2);
    audio.clear();
  });
  it('discards malformed PCM and still lets the following valid segment play', async () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    await audio.unlock();
    expect(() =>
      audio.chunk({ segment_id: 'invalid', agent_id: 'a', chunk_index: 0, audio: btoa('x') }),
    ).not.toThrow();
    audio.chunk({ segment_id: 'next', agent_id: 'b', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'next', total_samples: 2 });
    await vi.runAllTimersAsync();
    expect(ack.mock.calls).toEqual([
      ['invalid', 0, true],
      ['next', 2, false],
    ]);
    expect(() => decodePCM(btoa('x'))).toThrow('incomplete sample');
  });
  it('finishes a browser utterance once even if both error and end fire', async () => {
    const ack = vi.fn(),
      speaking = vi.fn(),
      audio = new BroadcastAudio(speaking, ack);
    await audio.unlock();
    audio.demo('The lead.', 'a', 'lead');
    audio.demo('The reply.', 'b', 'peer');
    const speak = vi.mocked(window.speechSynthesis.speak),
      lead = speak.mock.calls[0][0] as unknown as Utterance;
    lead.onerror?.();
    const peer = speak.mock.calls[1][0] as unknown as Utterance;
    peer.onstart?.();
    lead.onend?.();
    expect(ack).toHaveBeenCalledExactlyOnceWith('lead', 0, true);
    expect(speaking).toHaveBeenLastCalledWith('b');
    expect(speak).toHaveBeenCalledTimes(2);
    peer.onend?.();
    expect(ack.mock.calls).toEqual([
      ['lead', 0, true],
      ['peer', 0, false],
    ]);
  });
  it('mutes blocked browser speech and cancels queued lines with an actionable issue', async () => {
    const ack = vi.fn(),
      issue = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack, issue);
    await audio.unlock();
    audio.demo('The lead.', 'a', 'blocked');
    audio.demo('The reply.', 'b', 'queued');
    const utterance = vi.mocked(window.speechSynthesis.speak).mock
      .calls[0][0] as unknown as Utterance;
    utterance.onerror?.({ error: 'not-allowed' });
    utterance.onend?.();
    expect(ack.mock.calls).toEqual([
      ['blocked', 0, true],
      ['queued', 0, true],
    ]);
    expect(issue).toHaveBeenCalledWith(expect.stringContaining('Allow sound for this site'), true);
    audio.demo('Another call.', 'a', 'muted');
    expect(window.speechSynthesis.speak).toHaveBeenCalledOnce();
    expect(ack).toHaveBeenLastCalledWith('muted', 0, true);
    await audio.unlock();
    audio.demo('A fresh call.', 'a', 'retry');
    expect(window.speechSynthesis.speak).toHaveBeenCalledTimes(2);
    audio.clear();
  });
  it('reports an unavailable speech API instead of silently discarding demo commentary', async () => {
    vi.stubGlobal('SpeechSynthesisUtterance', undefined);
    const ack = vi.fn(),
      issue = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack, issue);
    await audio.unlock();
    audio.demo('A lead.', 'a', 'unavailable');
    expect(ack).toHaveBeenCalledExactlyOnceWith('unavailable', 0, true);
    expect(issue).toHaveBeenCalledWith(expect.stringContaining('does not support'), true);
    expect(window.speechSynthesis.speak).not.toHaveBeenCalled();
  });
  it.each(['voice-unavailable', 'language-unavailable', 'synthesis-unavailable'])(
    'mutes %s with instructions to install an English speech voice',
    async (error) => {
      const ack = vi.fn(),
        issue = vi.fn(),
        audio = new BroadcastAudio(vi.fn(), ack, issue);
      await audio.unlock();
      audio.demo('A lead.', 'a', 'unavailable');
      const utterance = vi.mocked(window.speechSynthesis.speak).mock
        .calls[0][0] as unknown as Utterance;
      utterance.onerror?.({ error });
      expect(ack).toHaveBeenCalledExactlyOnceWith('unavailable', 0, true);
      expect(issue).toHaveBeenCalledWith(expect.stringContaining('English speech voice'), true);
      audio.demo('A reply.', 'b', 'muted');
      expect(window.speechSynthesis.speak).toHaveBeenCalledOnce();
    },
  );
  it('plays a local voice preview without sending a playback acknowledgement to the server', async () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    await audio.testVoice();
    const speak = vi.mocked(window.speechSynthesis.speak),
      utterance = speak.mock.calls[0][0] as unknown as Utterance;
    expect(utterance.text).toBe('Commentary voice is ready.');
    utterance.onstart?.();
    utterance.onend?.();
    expect(ack).not.toHaveBeenCalled();
    await audio.testVoice();
    audio.clear();
    expect(ack).not.toHaveBeenCalled();
    expect(speak).toHaveBeenCalledTimes(2);
  });
  it('queues a voice preview after broadcast speech without interrupting it', async () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    await audio.unlock();
    audio.demo('An ongoing call.', 'b', 'broadcast');
    await audio.testVoice();
    const speak = vi.mocked(window.speechSynthesis.speak),
      broadcast = speak.mock.calls[0][0] as unknown as Utterance;
    expect(speak).toHaveBeenCalledOnce();
    expect(window.speechSynthesis.cancel).not.toHaveBeenCalled();
    broadcast.onend?.();
    const preview = speak.mock.calls[1][0] as unknown as Utterance;
    preview.onend?.();
    expect(ack).toHaveBeenCalledExactlyOnceWith('broadcast', 0, false);
  });
  it('ignores cancelled browser callbacks and duplicate demo calls', async () => {
    const ack = vi.fn(),
      speaking = vi.fn(),
      audio = new BroadcastAudio(speaking, ack);
    await audio.unlock();
    audio.demo('A call.', 'a', 'cancelled');
    const utterance = vi.mocked(window.speechSynthesis.speak).mock
      .calls[0][0] as unknown as Utterance;
    audio.clear();
    utterance.onstart?.();
    utterance.onend?.();
    audio.demo('A call.', 'a', 'cancelled');
    expect(ack).toHaveBeenCalledExactlyOnceWith('cancelled', 0, true);
    expect(speaking).toHaveBeenLastCalledWith(null);
    expect(window.speechSynthesis.speak).toHaveBeenCalledTimes(1);
  });
  it('keeps browser speech and PCM from talking over each other', async () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    await audio.unlock();
    audio.demo('The lead.', 'a', 'demo');
    audio.chunk({ segment_id: 'pcm', agent_id: 'b', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'pcm', total_samples: 2 });
    expect(scheduled).toHaveLength(0);
    const utterance = vi.mocked(window.speechSynthesis.speak).mock
      .calls[0][0] as unknown as Utterance;
    utterance.onend?.();
    expect(scheduled).toHaveLength(1);
    await vi.runAllTimersAsync();
    expect(ack.mock.calls).toEqual([
      ['demo', 0, false],
      ['pcm', 2, false],
    ]);
  });
  it('cancels sealed audio with a missing chunk and releases its peer', async () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    await audio.unlock();
    audio.chunk({ segment_id: 'gap', agent_id: 'a', chunk_index: 1, audio: pcm([1, 1]) });
    audio.chunk({ segment_id: 'peer', agent_id: 'b', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'peer', total_samples: 2 });
    audio.seal({ segment_id: 'gap', total_samples: 2 });
    await vi.runAllTimersAsync();
    expect(ack.mock.calls).toEqual([
      ['gap', 0, true],
      ['peer', 2, false],
    ]);
  });
  it('rejects a sample count mismatch instead of waiting for nonexistent audio', async () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    await audio.unlock();
    audio.chunk({ segment_id: 'short', agent_id: 'a', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'short', total_samples: 4 });
    await vi.runAllTimersAsync();
    expect(ack).toHaveBeenCalledExactlyOnceWith('short', 0, true);
  });
  it('keeps audio muted after the browser rejects resume', async () => {
    class BlockedContext extends Context {
      async resume() {
        throw new Error('Autoplay denied');
      }
    }
    vi.stubGlobal('AudioContext', BlockedContext);
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    await expect(audio.unlock()).rejects.toThrow('Autoplay denied');
    audio.chunk({ segment_id: 'muted', agent_id: 'a', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'muted', total_samples: 2 });
    expect(scheduled).toHaveLength(0);
    expect(ack).toHaveBeenCalledExactlyOnceWith('muted', 0, true);
  });
  it('does not let a delayed unlock undo a later mute', async () => {
    let resume: (() => void) | undefined;
    class SlowContext extends Context {
      resume() {
        return new Promise<void>((resolve) => {
          resume = resolve;
        });
      }
    }
    vi.stubGlobal('AudioContext', SlowContext);
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack),
      unlock = audio.unlock();
    audio.mute(true);
    resume?.();
    await unlock;
    audio.chunk({ segment_id: 'muted', agent_id: 'a', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'muted', total_samples: 2 });
    expect(scheduled).toHaveLength(0);
    expect(ack).toHaveBeenCalledExactlyOnceWith('muted', 0, true);
  });
  it('recovers the queue if the speech engine never starts a line', async () => {
    const ack = vi.fn(),
      issue = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack, issue);
    await audio.unlock();
    audio.demo('A lead call.', 'a', 'lead');
    audio.demo('A peer reply.', 'b', 'peer');
    await vi.advanceTimersByTimeAsync(8000);
    expect(ack).toHaveBeenCalledExactlyOnceWith('lead', 0, true);
    expect(window.speechSynthesis.cancel).toHaveBeenCalledOnce();
    expect(window.speechSynthesis.speak).toHaveBeenCalledTimes(2);
    expect(issue).toHaveBeenCalledWith(expect.stringContaining('stopped responding'), false);
    const peer = vi.mocked(window.speechSynthesis.speak).mock.calls[1][0] as unknown as Utterance;
    peer.onstart?.();
    peer.onend?.();
    expect(ack.mock.calls).toEqual([
      ['lead', 0, true],
      ['peer', 0, false],
    ]);
  });
  it('cancels speech that starts but never reports completion', async () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    await audio.unlock();
    audio.demo('A short line.', 'a', 'stalled');
    const utterance = vi.mocked(window.speechSynthesis.speak).mock
      .calls[0][0] as unknown as Utterance;
    utterance.onstart?.();
    await vi.advanceTimersByTimeAsync(10000);
    expect(ack).toHaveBeenCalledExactlyOnceWith('stalled', 0, true);
    utterance.onend?.();
    expect(ack).toHaveBeenCalledOnce();
  });
  it('uses the default voice if voice enumeration fails', async () => {
    vi.spyOn(window.speechSynthesis, 'getVoices').mockImplementation(() => {
      throw new Error('Voices unavailable');
    });
    const ack = vi.fn(),
      issue = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack, issue);
    await audio.unlock();
    expect(() => audio.demo('A call.', 'a', 'demo')).not.toThrow();
    const utterance = vi.mocked(window.speechSynthesis.speak).mock
      .calls[0][0] as unknown as Utterance;
    expect(utterance.voice).toBe(null);
    expect(issue).toHaveBeenCalledWith(expect.stringContaining('default voice'), false);
    utterance.onend?.();
    expect(ack).toHaveBeenCalledExactlyOnceWith('demo', 0, false);
  });
  it.each([true, false])(
    'wakes a paused native speech engine after enqueueing when the browser reports paused=%s',
    async (paused) => {
      let queued: Utterance | undefined;
      const speaking = vi.fn(),
        ack = vi.fn(),
        resume = vi.fn(() => {
          // Like Chromium, an idle resume does not reach its native engine.
          if (!queued) return;
          Object.assign(window.speechSynthesis, { paused: false });
          queued.onstart?.();
        });
      Object.assign(window.speechSynthesis, { paused, resume });
      vi.mocked(window.speechSynthesis.speak).mockImplementation((utterance) => {
        // The native engine can be paused even while the JS getter is false.
        queued = utterance as unknown as Utterance;
      });
      const audio = new BroadcastAudio(speaking, ack);
      await audio.unlock();
      audio.demo('The broadcast is back.', 'a', 'resumed');
      expect(resume).toHaveBeenCalledOnce();
      expect(speaking).toHaveBeenLastCalledWith('a');
      queued?.onend?.();
      expect(ack).toHaveBeenCalledExactlyOnceWith('resumed', 0, false);
    },
  );
  it('prefers installed English voices and sets the utterance language', async () => {
    const online = { lang: 'en-US', localService: false },
      installed = { lang: 'en-IE', localService: true },
      other = { lang: 'fr-FR', localService: true };
    vi.spyOn(window.speechSynthesis, 'getVoices').mockReturnValue([
      online,
      other,
      installed,
    ] as SpeechSynthesisVoice[]);
    const audio = new BroadcastAudio(vi.fn(), vi.fn());
    await audio.unlock();
    audio.demo('An English call.', 'a', 'english');
    const utterance = vi.mocked(window.speechSynthesis.speak).mock
      .calls[0][0] as unknown as Utterance;
    expect(utterance.voice).toBe(installed);
    expect(utterance.lang).toBe('en-IE');
    utterance.onend?.();
  });
  it('uses a sole installed voice for both commentators instead of an online peer voice', async () => {
    const online = { lang: 'en-US', localService: false },
      installed = { lang: 'en-IE', localService: true };
    vi.spyOn(window.speechSynthesis, 'getVoices').mockReturnValue([
      online,
      installed,
    ] as SpeechSynthesisVoice[]);
    const audio = new BroadcastAudio(vi.fn(), vi.fn());
    await audio.unlock();
    audio.demo('The lead.', 'a', 'lead');
    audio.demo('The peer.', 'b', 'peer');
    const speak = vi.mocked(window.speechSynthesis.speak),
      lead = speak.mock.calls[0][0] as unknown as Utterance;
    lead.onend?.();
    const peer = speak.mock.calls[1][0] as unknown as Utterance;
    expect(lead.voice).toBe(installed);
    expect(peer.voice).toBe(installed);
    expect(peer.rate).not.toBe(lead.rate);
    expect(peer.pitch).not.toBe(lead.pitch);
    peer.onend?.();
  });
  it('sets English for the default voice when the voice list has not loaded yet', async () => {
    const audio = new BroadcastAudio(vi.fn(), vi.fn());
    await audio.unlock();
    audio.demo('A clear call.', 'a', 'default');
    const utterance = vi.mocked(window.speechSynthesis.speak).mock
      .calls[0][0] as unknown as Utterance;
    expect(utterance.voice).toBe(null);
    expect(utterance.lang).toBe('en-US');
    utterance.onend?.();
  });
  it('contains utterance construction failures and releases ownership', async () => {
    vi.stubGlobal(
      'SpeechSynthesisUtterance',
      class {
        constructor() {
          throw new Error('Unavailable');
        }
      },
    );
    const ack = vi.fn(),
      issue = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack, issue);
    await audio.unlock();
    expect(() => audio.demo('A call.', 'a', 'failed')).not.toThrow();
    expect(ack).toHaveBeenCalledExactlyOnceWith('failed', 0, true);
    expect(issue).toHaveBeenCalledWith(expect.stringContaining('could not speak'), false);
  });
  it('finishes cleanup even when browser speech cancellation throws', async () => {
    vi.mocked(window.speechSynthesis.cancel).mockImplementation(() => {
      throw new Error('Cannot cancel');
    });
    const ack = vi.fn(),
      speaking = vi.fn(),
      issue = vi.fn(),
      audio = new BroadcastAudio(speaking, ack, issue);
    await audio.unlock();
    audio.demo('A lead.', 'a', 'lead');
    audio.demo('A reply.', 'b', 'peer');
    expect(() => audio.clear()).not.toThrow();
    expect(ack.mock.calls).toEqual([
      ['lead', 0, true],
      ['peer', 0, true],
    ]);
    expect(speaking).toHaveBeenLastCalledWith(null);
    expect(issue).toHaveBeenCalledWith(expect.stringContaining('Voice has been muted'), true);
    audio.demo('A later line.', 'a', 'later');
    expect(window.speechSynthesis.speak).toHaveBeenCalledOnce();
    expect(ack).toHaveBeenLastCalledWith('later', 0, true);
  });
  it('contains WebAudio scheduling errors and still plays a valid following segment', async () => {
    let fail = true;
    class UnstableContext extends Context {
      createBuffer(channels: number, length: number, rate: number) {
        if (fail) {
          fail = false;
          throw new Error('Audio device unavailable');
        }
        return super.createBuffer(channels, length, rate);
      }
    }
    vi.stubGlobal('AudioContext', UnstableContext);
    const ack = vi.fn(),
      issue = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack, issue);
    await audio.unlock();
    expect(() =>
      audio.chunk({ segment_id: 'failed', agent_id: 'a', chunk_index: 0, audio: pcm([1, 1]) }),
    ).not.toThrow();
    audio.chunk({ segment_id: 'next', agent_id: 'b', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'next', total_samples: 2 });
    await vi.runAllTimersAsync();
    expect(ack.mock.calls).toEqual([
      ['failed', 0, true],
      ['next', 2, false],
    ]);
    expect(issue).toHaveBeenCalledWith(expect.stringContaining('could not play'), false);
  });
  it('allows demo speech when only the speech synthesis API is available', async () => {
    vi.stubGlobal('AudioContext', undefined);
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    await audio.unlock();
    audio.demo('A call.', 'a', 'demo');
    const utterance = vi.mocked(window.speechSynthesis.speak).mock
      .calls[0][0] as unknown as Utterance;
    utterance.onend?.();
    expect(ack).toHaveBeenCalledExactlyOnceWith('demo', 0, false);
    audio.chunk({ segment_id: 'pcm', agent_id: 'b', chunk_index: 0, audio: pcm([1, 1]) });
    expect(ack).toHaveBeenLastCalledWith('pcm', 0, true);
  });
  it('does not strand later speakers when the acknowledgement transport throws', async () => {
    let fail = true;
    const ack = vi.fn(() => {
      if (fail) {
        fail = false;
        throw new Error('Socket closed');
      }
    });
    const issue = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack, issue);
    await audio.unlock();
    audio.demo('A lead.', 'a', 'lead');
    audio.demo('A peer.', 'b', 'peer');
    const speak = vi.mocked(window.speechSynthesis.speak),
      lead = speak.mock.calls[0][0] as unknown as Utterance;
    expect(() => lead.onend?.()).not.toThrow();
    const peer = speak.mock.calls[1][0] as unknown as Utterance;
    peer.onend?.();
    expect(ack.mock.calls).toEqual([
      ['lead', 0, false],
      ['peer', 0, false],
    ]);
    expect(issue).toHaveBeenCalledWith(expect.stringContaining('acknowledgement'), false);
  });
  it('times out an audio resume that never resolves', async () => {
    class NeverContext extends Context {
      resume() {
        return new Promise<void>(() => {});
      }
    }
    vi.stubGlobal('AudioContext', NeverContext);
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    const result = expect(audio.unlock()).rejects.toThrow('did not enable audio in time');
    await vi.advanceTimersByTimeAsync(8000);
    await result;
    audio.demo('A call.', 'a', 'muted');
    expect(ack).toHaveBeenCalledExactlyOnceWith('muted', 0, true);
  });
  it('rejects new PCM after a seal rather than extending completed speech', async () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    await audio.unlock();
    audio.chunk({ segment_id: 'sealed', agent_id: 'a', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'sealed', total_samples: 2 });
    audio.chunk({ segment_id: 'sealed', agent_id: 'a', chunk_index: 1, audio: pcm([1, 1]) });
    await vi.runAllTimersAsync();
    expect(scheduled).toHaveLength(1);
    expect(ack).toHaveBeenCalledExactlyOnceWith('sealed', 0, true);
  });
  it('cancels a segment if the audio device closes and recreates it on the next unlock', async () => {
    const ack = vi.fn(),
      issue = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack, issue);
    await audio.unlock();
    audio.chunk({ segment_id: 'closed', agent_id: 'a', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'closed', total_samples: 2 });
    Object.assign(contexts[0], { state: 'closed', frozen: 0 });
    await vi.runAllTimersAsync();
    expect(ack).toHaveBeenCalledExactlyOnceWith('closed', 0, true);
    expect(issue).toHaveBeenCalledWith(expect.stringContaining('device closed'), false);
    await audio.unlock();
    expect(contexts).toHaveLength(2);
    audio.chunk({ segment_id: 'next', agent_id: 'b', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'next', total_samples: 2 });
    await vi.runAllTimersAsync();
    expect(ack).toHaveBeenLastCalledWith('next', 2, false);
  });
});

describe('handoffs between hosts', () => {
  const second = (value: number) => pcm(Array(24000).fill(value));
  it('lets the partner jump in on the last word of a big moment and still stops the tail', async () => {
    const ack = vi.fn(),
      audio = new BroadcastAudio(vi.fn(), ack);
    await audio.unlock();
    audio.prepare('peer', 'game:1:x:play', delivery({ overlap_ms: 260, sound_cue: 'none' }));
    audio.chunk({ segment_id: 'lead', agent_id: 'a', chunk_index: 0, audio: second(1) });
    audio.seal({ segment_id: 'lead', total_samples: 24000 });
    audio.chunk({ segment_id: 'peer', agent_id: 'b', chunk_index: 0, audio: pcm([1, 1]) });
    audio.seal({ segment_id: 'peer', total_samples: 2 });
    const leadEnd = scheduled[0] + 1;
    await vi.advanceTimersByTimeAsync(900);
    expect(scheduled).toHaveLength(2);
    // The reply starts inside the lead's last quarter second, with no extra breath.
    expect(scheduled[1]).toBeLessThan(leadEnd);
    expect(scheduled[1]).toBeGreaterThan(leadEnd - 0.4);
    expect(ack).toHaveBeenCalledWith('lead', 24000, false);
    audio.clear();
    expect(stopped[0]).toHaveBeenCalled(); // pause still silences the overlapped tail
  });
  it('never overlaps the same host or an ordinary reply', async () => {
    for (const [agent, overlap] of [
      ['a', 260],
      ['b', 0],
    ] as const) {
      scheduled.length = 0;
      const audio = new BroadcastAudio(vi.fn(), vi.fn());
      await audio.unlock();
      audio.prepare('next', 'game:1:x:play', delivery({ overlap_ms: overlap, pause_before_ms: 0 }));
      audio.chunk({ segment_id: 'lead', agent_id: 'a', chunk_index: 0, audio: second(1) });
      audio.seal({ segment_id: 'lead', total_samples: 24000 });
      audio.chunk({ segment_id: 'next', agent_id: agent, chunk_index: 0, audio: pcm([1, 1]) });
      audio.seal({ segment_id: 'next', total_samples: 2 });
      const leadEnd = scheduled[0] + 1;
      await vi.runAllTimersAsync();
      expect(scheduled[1]).toBeGreaterThanOrEqual(leadEnd);
      audio.clear();
    }
  });
  it('reports a segment pending until it has been heard', async () => {
    const audio = new BroadcastAudio(vi.fn(), vi.fn());
    await audio.unlock();
    expect(audio.pending('lead')).toBe(false);
    audio.chunk({ segment_id: 'lead', agent_id: 'a', chunk_index: 0, audio: pcm([1, 1]) });
    expect(audio.pending('lead')).toBe(true);
    audio.seal({ segment_id: 'lead', total_samples: 2 });
    await vi.runAllTimersAsync();
    expect(audio.pending('lead')).toBe(false);
  });
  it('prefers natural neural browser voices, a different one for each host', async () => {
    const robotic = { lang: 'en-US', localService: true, name: 'Microsoft David - English' },
      aria = {
        lang: 'en-US',
        localService: false,
        name: 'Microsoft Aria Online (Natural) - English',
      },
      guy = {
        lang: 'en-US',
        localService: false,
        name: 'Microsoft Guy Online (Natural) - English',
      };
    vi.spyOn(window.speechSynthesis, 'getVoices').mockReturnValue([
      robotic,
      aria,
      guy,
    ] as SpeechSynthesisVoice[]);
    const audio = new BroadcastAudio(vi.fn(), vi.fn());
    await audio.unlock();
    audio.demo('The lead.', 'a', 'lead');
    audio.demo('The peer.', 'b', 'peer');
    const speak = vi.mocked(window.speechSynthesis.speak),
      lead = speak.mock.calls[0][0] as unknown as Utterance;
    lead.onend?.();
    await vi.runAllTimersAsync();
    const peer = speak.mock.calls[1][0] as unknown as Utterance;
    expect(lead.voice).toBe(aria);
    expect(peer.voice).toBe(guy);
    peer.onend?.();
  });
});
