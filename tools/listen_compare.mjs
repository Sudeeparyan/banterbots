/**
 * Record the same replay moments through the real dashboard and measure what a listener hears.
 *
 *   cd frontend && npm run build
 *   node tools/listen_compare.mjs --label before
 *   node tools/listen_compare.mjs --label after --scenarios td_bal,reversed_td
 *
 * Starts isolated services on ports 8100-8102 (override with --port), drives the dashboard in
 * Chromium, taps the browser's master output losslessly and writes, under .runtime/listening/<label>/:
 * one WAV per scenario, <scenario>.json (turns, timing, usage) and metrics.json + summary.md.
 * Expressive voices need Azure credentials in .env; pass --provider demo for browser speech
 * (OS speech is outside Web Audio, so Demo recordings contain atmosphere only).
 */
import { chromium, expect } from '../frontend/node_modules/@playwright/test/index.mjs';
import { spawn, spawnSync } from 'node:child_process';
import { createWriteStream } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const options = Object.fromEntries(
  process.argv
    .slice(2)
    .join(' ')
    .split(/\s*--/)
    .filter(Boolean)
    .map((item) => {
      const [key, ...value] = item.split(/\s+/);
      return [key, value.join(' ') || 'true'];
    }),
);
const label = options.label ?? 'after';
const port = Number(options.port ?? 8100);
const provider = options.provider ?? 'azure';
const speed = Number(options.speed ?? 2);
const game = options.game ?? '2024_01_BAL_KC';
// Replay indices in data/2024_01_BAL_KC.json. Plays are consecutive from index.
export const SCENARIOS = [
  { id: 'opening_drive', label: 'Quiet opening snaps into a third-down conversion', index: 1, plays: 5 },
  { id: 'third_down', label: 'Third-and-11 conversion', index: 5, plays: 1 },
  { id: 'td_bal', label: 'Baltimore touchdown (away)', index: 14, plays: 1 },
  { id: 'td_kc', label: 'Kansas City touchdown (home)', index: 21, plays: 1 },
  { id: 'turnover', label: 'Strip-sack fumble', index: 36, plays: 1 },
  { id: 'halftime', label: 'End of the first half', index: 88, plays: 1 },
  { id: 'long_gain', label: '33-yard completion', index: 91, plays: 1 },
  { id: 'reversed_td', label: 'Reversed Baltimore touchdown', index: 176, plays: 1 },
  { id: 'end_of_game', label: 'Final whistle', index: 177, plays: 1 },
];
const wanted = options.scenarios ? new Set(options.scenarios.split(',')) : null;
const scenarios = SCENARIOS.filter((item) => !wanted || wanted.has(item.id));
const output = path.join(root, '.runtime', 'listening', label);
const base = `http://127.0.0.1:${port}`;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const REACTION =
  /\b(oh+|ooh+|wow|whoa|yes+|yeah|no,? no|come on|ha(?:ha)*|hah|oof|ugh|boy|hey|unbelievable|are you kidding|my goodness|what a|look at|get in|go,? go)\b|!/i;

function words(text) {
  return (text ?? '').match(/[A-Za-z0-9']+/g)?.length ?? 0;
}

function quantile(values, q) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(q * sorted.length) - 1));
  return Math.round(sorted[index]);
}

function ngrams(text, size = 4) {
  const tokens = (text ?? '')
    .toLowerCase()
    .replace(/\d+/g, '#')
    .match(/[a-z#']+/g);
  const grams = new Set();
  for (let i = 0; tokens && i + size <= tokens.length; i++) grams.add(tokens.slice(i, i + size).join(' '));
  return grams;
}

function wav(int16, rate, channels) {
  const header = Buffer.alloc(44);
  header.write('RIFF', 0);
  header.writeUInt32LE(36 + int16.length, 4);
  header.write('WAVE', 8);
  header.write('fmt ', 12);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(channels, 22);
  header.writeUInt32LE(rate, 24);
  header.writeUInt32LE(rate * channels * 2, 28);
  header.writeUInt16LE(channels * 2, 32);
  header.writeUInt16LE(16, 34);
  header.write('data', 36);
  header.writeUInt32LE(int16.length, 40);
  return Buffer.concat([header, int16]);
}

async function services() {
  const runtime = path.join(output, 'runtime');
  await mkdir(runtime, { recursive: true });
  const python = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const env = {
    ...process.env,
    BANTERBOTS_RUNTIME: runtime,
    AGENT_A_URL: `http://127.0.0.1:${port + 1}`,
    AGENT_B_URL: `http://127.0.0.1:${port + 2}`,
  };
  const jobs = [
    ['agent-a', ['-m', 'backend.agents', '--agent', 'a', '--port', String(port + 1)]],
    ['agent-b', ['-m', 'backend.agents', '--agent', 'b', '--port', String(port + 2)]],
    ['api', ['-m', 'uvicorn', 'backend.api:app', '--host', '127.0.0.1', '--port', String(port)]],
  ];
  const children = jobs.map(([name, args]) => {
    const child = spawn(python, args, { cwd: root, env, windowsHide: true });
    const log = createWriteStream(path.join(runtime, `${name}.log`));
    child.stdout.pipe(log);
    child.stderr.pipe(log);
    return child;
  });
  const deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    try {
      const health = await (await fetch(`${base}/api/health`)).json();
      if (health.status === 'ok') return children;
    } catch {
      /* still starting */
    }
    await sleep(500);
  }
  stop(children);
  throw new Error(`Services did not become healthy; see ${runtime}`);
}

function stop(children) {
  for (const child of children) {
    if (child.exitCode !== null) continue;
    if (process.platform === 'win32') spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F']);
    else child.kill();
  }
}

// Runs in the page before the app: a lossless tap of everything sent to the speakers.
function installTap() {
  window.__banterTimeline = [];
  const connect = AudioNode.prototype.connect;
  const taps = new WeakMap();
  const recording = { chunks: [], rate: 0, active: false };
  window.__tap = recording;
  const tapFor = (context) => {
    if (taps.has(context)) return taps.get(context);
    const processor = context.createScriptProcessor(4096, 2, 2);
    processor.onaudioprocess = (event) => {
      if (!recording.active) return;
      recording.rate = context.sampleRate;
      recording.chunks.push([
        new Float32Array(event.inputBuffer.getChannelData(0)),
        new Float32Array(event.inputBuffer.getChannelData(1)),
      ]);
    };
    connect.call(processor, context.destination);
    taps.set(context, processor);
    return processor;
  };
  AudioNode.prototype.connect = function (target, ...rest) {
    const result = connect.call(this, target, ...rest);
    if (target instanceof AudioDestinationNode && !(this instanceof ScriptProcessorNode)) {
      connect.call(this, tapFor(this.context));
    }
    return result;
  };
  window.__tapStop = () => {
    recording.active = false;
    const length = recording.chunks.reduce((sum, [left]) => sum + left.length, 0);
    const pcm = new Int16Array(length * 2);
    let offset = 0;
    let peak = 0;
    for (const [left, right] of recording.chunks) {
      for (let i = 0; i < left.length; i++) {
        peak = Math.max(peak, Math.abs(left[i]), Math.abs(right[i]));
        pcm[offset++] = Math.max(-1, Math.min(1, left[i])) * 32767;
        pcm[offset++] = Math.max(-1, Math.min(1, right[i])) * 32767;
      }
    }
    const bytes = new Uint8Array(pcm.buffer);
    let binary = '';
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return { rate: recording.rate || 48000, base64: btoa(binary), peak };
  };
}

async function record(browser, scenario, replay) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const frames = [];
  const sent = [];
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('websocket', (socket) => {
    socket.on('framereceived', ({ payload }) => {
      try {
        const message = JSON.parse(payload.toString());
        if (message.type !== 'audio' && !message.historical) frames.push({ ...message, received: Date.now() });
      } catch {
        /* binary or malformed */
      }
    });
    socket.on('framesent', ({ payload }) => {
      try {
        sent.push(JSON.parse(payload.toString()));
      } catch {
        /* ignore */
      }
    });
  });
  await page.addInitScript(installTap);
  await page.route('**/api/sessions', async (route) => {
    const request = route.request();
    if (request.method() !== 'POST') return route.continue();
    const body = JSON.parse(request.postData() ?? '{}');
    await route.continue({
      postData: JSON.stringify({ ...body, game_id: game, start_index: scenario.index, speed }),
      headers: { ...request.headers(), 'content-type': 'application/json' },
    });
  });
  const targets = new Set(replay.slice(scenario.index, scenario.index + scenario.plays).map((play) => play.id));
  const started = Date.now();
  try {
    await page.goto(base);
    const advance = page.getByRole('button', { name: 'Advance one play' });
    await expect(advance).toBeEnabled({ timeout: 60000 });
    if (provider === 'azure') {
      await page.getByRole('button', { name: 'Use expressive voices' }).click();
      await expect(page.getByLabel('Commentary provider')).toHaveValue('azure');
      await expect(advance).toBeEnabled({ timeout: 60000 });
    }
    await page.getByRole('button', { name: 'Enable commentary voice', exact: true }).click();
    await page.evaluate(() => (window.__tap.active = true));
    if (scenario.plays === 1) await advance.click();
    else await page.getByRole('button', { name: 'Start broadcast' }).click();
    const timeout = Date.now() + 150000 * scenario.plays;
    let settledAt = null;
    for (;;) {
      if (Date.now() > timeout) throw new Error(`Timed out waiting for ${scenario.id}`);
      const starts = frames.filter((frame) => frame.type === 'turn.start' && targets.has(frame.data.event_id));
      const acked = new Set(sent.filter((item) => item.type === 'playback.ack').map((item) => item.segment_id));
      const state = frames.filter((frame) => frame.type === 'state').at(-1)?.data;
      const moved = state && (state.index >= scenario.index + scenario.plays || ['paused', 'completed'].includes(state.status));
      const finished = starts.length && starts.every((frame) => acked.has(frame.data.segment_id)) && moved;
      const failed = frames.find((frame) => frame.type === 'error' && frame.data.component === 'orchestrator');
      if (failed) throw new Error(failed.data.message);
      if (finished) {
        settledAt ??= Date.now();
        if (Date.now() - settledAt > 1500) break;
      } else settledAt = null;
      await sleep(250);
    }
    await sleep(600);
    const tap = await page.evaluate(() => window.__tapStop());
    const timeline = await page.evaluate(() => window.__banterTimeline);
    const pause = page.getByRole('button', { name: 'Pause broadcast' });
    if (await pause.isVisible().catch(() => false)) await pause.click().catch(() => {});
    const pcm = Buffer.from(tap.base64, 'base64');
    await writeFile(path.join(output, `${scenario.id}.wav`), wav(pcm, tap.rate, 2));
    return analyse(scenario, targets, frames, sent, timeline, errors, Date.now() - started, tap);
  } finally {
    await context.close();
  }
}

function analyse(scenario, targets, frames, sent, timeline, errors, elapsed, tap) {
  const turns = new Map();
  for (const frame of frames) if (frame.type === 'turn') turns.set(frame.data.turn.id, frame.data.turn);
  const starts = frames.filter((frame) => frame.type === 'turn.start' && targets.has(frame.data.event_id));
  const segments = [];
  for (const start of starts) {
    const id = start.data.segment_id;
    const marks = timeline.filter((item) => item.segment === id);
    const voice = marks.filter((item) => item.type === 'voice');
    const audible = voice.filter((item) => item.first_ms !== null);
    const speech = marks.filter((item) => item.type === 'speech_start' || item.type === 'speech_end');
    const begin = audible.length
      ? Math.min(...audible.map((item) => item.first_ms))
      : speech.length
        ? Math.min(...speech.map((item) => item.at))
        : null;
    const end = audible.length
      ? Math.max(...audible.map((item) => item.last_ms))
      : speech.length
        ? Math.max(...speech.map((item) => item.at))
        : null;
    const segmentEnd = voice.length ? Math.max(...voice.map((item) => item.start_ms + item.duration_ms)) : end;
    const turn = turns.get(id) ?? {};
    const text = turn.spoken_text || turn.text || '';
    segments.push({
      id,
      agent: start.data.agent_id,
      persona: start.data.persona,
      event_id: start.data.event_id,
      exchange_id: start.data.exchange_id,
      kind: turn.kind ?? null,
      text,
      planned: turn.text ?? null,
      delivery: start.data.delivery ?? null,
      audible_start_ms: begin,
      audible_end_ms: end,
      segment_end_ms: segmentEnd,
      speech_seconds: begin !== null && end !== null ? (end - begin) / 1000 : 0,
      trailing_silence_ms: segmentEnd !== null && end !== null ? Math.round(segmentEnd - end) : null,
      words: words(text),
      reaction: REACTION.test(text),
      callback_to: turn.callback_to ?? null,
      fact_flag: turn.fact_flag ?? null,
    });
  }
  segments.sort((a, b) => (a.audible_start_ms ?? Infinity) - (b.audible_start_ms ?? Infinity));
  const earlier = [];
  for (const segment of segments) {
    const grams = ngrams(segment.text);
    segment.echo_callback = earlier.some(
      (item) => item.exchange_id !== segment.exchange_id && [...grams].some((gram) => item.grams.has(gram)),
    );
    earlier.push({ exchange_id: segment.exchange_id, grams });
  }
  const gaps = [];
  for (let i = 1; i < segments.length; i++) {
    const previous = segments[i - 1],
      next = segments[i];
    if (previous.audible_end_ms === null || next.audible_start_ms === null) continue;
    gaps.push({
      kind:
        previous.event_id !== next.event_id
          ? 'between_plays'
          : previous.agent !== next.agent
            ? 'handoff'
            : 'same_host',
      from: previous.id,
      to: next.id,
      ms: Math.round(next.audible_start_ms - previous.audible_end_ms),
    });
  }
  const traces = frames.filter((frame) => frame.type === 'trace').map((frame) => frame.data);
  const voiceSeconds = traces
    .filter((item) => item.node === 'speak' && item.phase === 'finalized')
    .reduce((sum, item) => sum + Number(item.usage?.seconds ?? item.usage?.duration_seconds ?? 0), 0)
    + traces
      .filter((item) => item.node === 'voice_session' && item.phase === 'usage')
      .reduce((sum, item) => sum + Number(item.seconds ?? 0), 0);
  const tokens = traces
    .filter((item) => item.node === 'generate' && item.usage)
    .reduce(
      (sum, item) => ({
        input: sum.input + (item.usage.input_tokens ?? 0),
        cached: sum.cached + (item.usage.input_tokens_details?.cached_tokens ?? 0),
        output: sum.output + (item.usage.output_tokens ?? 0),
      }),
      { input: 0, cached: 0, output: 0 },
    );
  const speechSeconds = segments.reduce((sum, item) => sum + item.speech_seconds, 0);
  const totalWords = segments.reduce((sum, item) => sum + item.words, 0);
  const recordedSeconds = tap.base64 ? Buffer.from(tap.base64, 'base64').length / 4 / tap.rate : 0;
  return {
    scenario: scenario.id,
    label: scenario.label,
    plays: scenario.plays,
    elapsed_ms: elapsed,
    recorded_seconds: Math.round(recordedSeconds * 10) / 10,
    peak: tap.peak,
    segments,
    gaps,
    words: totalWords,
    speech_seconds: Math.round(speechSeconds * 10) / 10,
    wpm: speechSeconds ? Math.round((totalWords / speechSeconds) * 60) : null,
    voice_session_seconds: Math.round(voiceSeconds * 10) / 10,
    text_tokens: tokens,
    errors: [...errors, ...frames.filter((frame) => frame.type === 'error').map((frame) => frame.data.message)],
  };
}

function summarise(results) {
  const gaps = results.flatMap((item) => item.gaps);
  const segments = results.flatMap((item) => item.segments);
  const speech = results.reduce((sum, item) => sum + item.speech_seconds, 0);
  const totalWords = results.reduce((sum, item) => sum + item.words, 0);
  const plays = results.reduce((sum, item) => sum + item.plays, 0);
  const sessionSeconds = results.reduce((sum, item) => sum + item.voice_session_seconds, 0);
  const tokens = results.reduce(
    (sum, item) => ({
      input: sum.input + item.text_tokens.input,
      cached: sum.cached + item.text_tokens.cached,
      output: sum.output + item.text_tokens.output,
    }),
    { input: 0, cached: 0, output: 0 },
  );
  const of = (kind) => gaps.filter((gap) => gap.kind === kind).map((gap) => gap.ms);
  // GPT-Live list price $0.05/min; gpt-6-luna $0.10 input, $0.01 cached, $0.50 output per 1M tokens.
  const voicePerPlay = plays ? (sessionSeconds / 60) * 0.05 / plays : 0;
  const textPerPlay = plays
    ? ((tokens.input - tokens.cached) * 0.1 + tokens.cached * 0.01 + tokens.output * 0.5) / 1e6 / plays
    : 0;
  return {
    label,
    provider,
    speed,
    recorded_at: new Date().toISOString(),
    plays,
    lines: segments.length,
    handoff_gap_ms: { median: quantile(of('handoff'), 0.5), p90: quantile(of('handoff'), 0.9), n: of('handoff').length },
    between_plays_gap_ms: {
      median: quantile(of('between_plays'), 0.5),
      p90: quantile(of('between_plays'), 0.9),
      n: of('between_plays').length,
    },
    // One host twice in a row on the same play: the pre-snap setup into the call.
    same_host_gap_ms: {
      median: quantile(of('same_host'), 0.5),
      p90: quantile(of('same_host'), 0.9),
      n: of('same_host').length,
    },
    trailing_silence_ms: quantile(segments.map((item) => item.trailing_silence_ms ?? 0), 0.5),
    words_per_minute: speech ? Math.round((totalWords / speech) * 60) : null,
    words_per_play: plays ? Math.round(totalWords / plays) : null,
    reaction_share: segments.length ? Math.round((segments.filter((item) => item.reaction).length / segments.length) * 100) / 100 : null,
    callback_share: segments.length
      ? Math.round((segments.filter((item) => item.callback_to || item.echo_callback).length / segments.length) * 100) / 100
      : null,
    fact_flags: segments.filter((item) => item.fact_flag).length,
    voice_session_seconds: Math.round(sessionSeconds),
    text_tokens: tokens,
    cost_per_play_usd: Math.round((voicePerPlay + textPerPlay) * 10000) / 10000,
    cost_per_178_play_game_usd: Math.round((voicePerPlay + textPerPlay) * 178 * 100) / 100,
    scenarios: results.map((item) => ({
      id: item.scenario,
      recorded_seconds: item.recorded_seconds,
      wpm: item.wpm,
      gaps: item.gaps.map((gap) => `${gap.kind}:${gap.ms}`),
      lines: item.segments.map((segment) => `${segment.persona}: ${segment.text}`),
      errors: item.errors,
    })),
  };
}

async function main() {
  await mkdir(output, { recursive: true });
  const replay = JSON.parse(await readFile(path.join(root, 'data', `${game}.json`), 'utf8'));
  const children = await services();
  const browser = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] });
  const results = [];
  try {
    for (const scenario of scenarios) {
      process.stdout.write(`${scenario.id}... `);
      try {
        const result = await record(browser, scenario, replay);
        await writeFile(path.join(output, `${scenario.id}.json`), JSON.stringify(result, null, 2));
        results.push(result);
        console.log(`${result.recorded_seconds}s recorded, gaps ${result.gaps.map((gap) => `${gap.kind}:${gap.ms}`).join(' ')}`);
      } catch (error) {
        console.log(`failed: ${error.message}`);
        await writeFile(path.join(output, `${scenario.id}.error.txt`), String(error.stack ?? error));
      }
    }
  } finally {
    await browser.close();
    stop(children);
  }
  // A partial re-run (--scenarios) keeps the label's other recordings in the summary.
  for (const scenario of SCENARIOS) {
    if (results.some((item) => item.scenario === scenario.id)) continue;
    try {
      results.push(JSON.parse(await readFile(path.join(output, `${scenario.id}.json`), 'utf8')));
    } catch {
      /* not recorded under this label */
    }
  }
  const summary = summarise(results);
  await writeFile(path.join(output, 'metrics.json'), JSON.stringify(summary, null, 2));
  console.log(JSON.stringify({ ...summary, scenarios: undefined }, null, 2));
}

if (import.meta.url === `file://${process.argv[1].replaceAll('\\', '/')}` || process.argv[1]?.endsWith('listen_compare.mjs')) {
  await main();
}
