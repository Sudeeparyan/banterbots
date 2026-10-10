/**
 * Measure loudness the way podcast platforms do (ITU-R BS.1770-4), with no ffmpeg or numpy:
 * Chromium decodes each file at 48 kHz, then this script applies K-weighting, gating and a
 * 4x-oversampled true-peak estimate.
 *
 *   node tools/measure_loudness.mjs <file.mp3|file.wav> [...more]
 *   node tools/measure_loudness.mjs --manifest frontend/public/audio/manifest.json
 *
 * Prints integrated LUFS, true peak (dBTP), duration and a 0.5 s loudness envelope per file.
 * With --manifest, each layer's gain_db is rewritten so its trimmed region sits at the
 * layer's target_lufs (the stadium mix then balances by design, not by ear on one device).
 */
import { chromium } from '../frontend/node_modules/@playwright/test/index.mjs';
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';

const RATE = 48000;
// BS.1770 K-weighting at 48 kHz: a high shelf (head effect) then a high pass (RLB curve).
const SHELF = { b: [1.53512485958697, -2.69169618940638, 1.19839281085285], a: [-1.69065929318241, 0.73248077421585] };
const HIGHPASS = { b: [1, -2, 1], a: [-1.99004745483398, 0.99007225036621] };

function biquad(input, { b, a }) {
  const output = new Float64Array(input.length);
  let x1 = 0, x2 = 0, y1 = 0, y2 = 0;
  for (let i = 0; i < input.length; i++) {
    const x = input[i], y = b[0] * x + b[1] * x1 + b[2] * x2 - a[0] * y1 - a[1] * y2;
    x2 = x1; x1 = x; y2 = y1; y1 = y;
    output[i] = y;
  }
  return output;
}

/** Integrated loudness with the absolute (-70 LUFS) and relative (-10 LU) gates. */
export function integratedLoudness(channels, rate = RATE) {
  const weighted = channels.map((samples) => biquad(biquad(samples, SHELF), HIGHPASS));
  const block = Math.round(0.4 * rate), step = Math.round(0.1 * rate), powers = [];
  for (let start = 0; start + block <= weighted[0].length; start += step) {
    let power = 0;
    for (const samples of weighted) {
      let sum = 0;
      for (let i = start; i < start + block; i++) sum += samples[i] * samples[i];
      power += sum / block;
    }
    powers.push(power);
  }
  const lufs = (power) => -0.691 + 10 * Math.log10(power);
  const absolute = powers.filter((power) => lufs(power) > -70);
  if (!absolute.length) return -Infinity;
  const mean = (values) => values.reduce((sum, value) => sum + value, 0) / values.length;
  const relative = absolute.filter((power) => lufs(power) > lufs(mean(absolute)) - 10);
  return lufs(mean(relative));
}

/** Peak of a 4x windowed-sinc oversampled signal, in dBTP. */
export function truePeak(channels) {
  const taps = 12, kernel = [];
  for (let phase = 1; phase < 4; phase++) {
    const weights = [];
    for (let k = -taps; k <= taps; k++) {
      const t = k - phase / 4;
      const sinc = Math.sin(Math.PI * t) / (Math.PI * t);
      const window = 0.5 + 0.5 * Math.cos((Math.PI * t) / (taps + 1));
      weights.push(sinc * window);
    }
    kernel.push(weights);
  }
  let peak = 0;
  for (const samples of channels) {
    for (let i = 0; i < samples.length; i++) {
      peak = Math.max(peak, Math.abs(samples[i]));
      for (const weights of kernel) {
        let value = 0;
        for (let k = -taps; k <= taps; k++) {
          const j = i + k + 1;
          if (j >= 0 && j < samples.length) value += samples[j] * weights[k + taps];
        }
        peak = Math.max(peak, Math.abs(value));
      }
    }
  }
  return 20 * Math.log10(peak || 1e-9);
}

function envelope(channels, rate = RATE, seconds = 0.5) {
  const size = Math.round(rate * seconds), values = [];
  for (let start = 0; start < channels[0].length; start += size) {
    let sum = 0, count = 0;
    for (const samples of channels)
      for (let i = start; i < Math.min(samples.length, start + size); i++, count++) sum += samples[i] * samples[i];
    values.push(Math.round(10 * Math.log10(sum / Math.max(1, count) || 1e-12)));
  }
  return values;
}

async function decoder() {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.setContent('<!doctype html><title>decode</title>');
  const decode = async (file) => {
    const bytes = await readFile(file);
    const channels = await page.evaluate(async ({ data, rate }) => {
      const raw = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
      const context = new OfflineAudioContext(2, rate, rate);
      const buffer = await context.decodeAudioData(raw.buffer);
      return Array.from({ length: buffer.numberOfChannels }, (_, c) => Array.from(buffer.getChannelData(c)));
    }, { data: bytes.toString('base64'), rate: RATE });
    return channels.map((values) => Float64Array.from(values));
  };
  return { decode, close: () => browser.close() };
}

export function measure(channels, start = 0, end = Infinity) {
  const from = Math.max(0, Math.round(start * RATE)), to = Math.min(channels[0].length, Math.round(end * RATE));
  const region = channels.map((samples) => samples.subarray(from, to));
  return {
    duration_s: Math.round((channels[0].length / RATE) * 100) / 100,
    integrated_lufs: Math.round(integratedLoudness(region) * 10) / 10,
    true_peak_dbtp: Math.round(truePeak(region) * 10) / 10,
  };
}

const args = process.argv.slice(2);
if (args.length) {
  const { decode, close } = await decoder();
  try {
    if (args[0] === '--manifest') {
      const file = path.resolve(args[1]);
      const manifest = JSON.parse(await readFile(file, 'utf8'));
      for (const layer of [...Object.values(manifest.layers), ...Object.values(manifest.stingers ?? {})]) {
        const channels = await decode(path.join(path.dirname(file), layer.file));
        const stats = measure(channels, layer.start ?? 0, layer.end ?? Infinity);
        layer.measured_lufs = stats.integrated_lufs;
        layer.gain_db = Math.round((layer.target_lufs - stats.integrated_lufs) * 10) / 10;
        console.log(JSON.stringify({ file: layer.file, ...stats, gain_db: layer.gain_db }));
      }
      await writeFile(file, JSON.stringify(manifest, null, 2) + '\n');
    } else {
      for (const file of args) {
        const channels = await decode(file);
        console.log(JSON.stringify({ file: path.basename(file), ...measure(channels), envelope_db: envelope(channels) }));
      }
    }
  } finally {
    await close();
  }
}
