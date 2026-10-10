# Audio sources and licenses

Every audio file the dashboard plays is listed here. Two kinds are shipped:

- **Recordings** (`crowd/`). Real crowds, each released by its author under
  [Creative Commons 0](https://creativecommons.org/publicdomain/zero/1.0/) (public domain dedication).
  The license was checked on each sound's Freesound page on 2026-10-10, just before download; every
  page links only the CC0 deed. The files are Freesound's high-quality MP3 previews and are byte-identical
  to what was downloaded (SHA-256 below). CC0 needs no attribution; the authors are credited anyway.
- **Designed sound** (`stingers/`). Not recordings. These are synthesized by `tools/make_stingers.py`
  from sine partials, envelopes and a seeded noise burst. They are original to this project and covered by the
  repository's own terms.

No file imitates a real broadcaster, team, league or stadium announcer. No recording contains intelligible
words except the general crowd reaction "oh". A chant layer was left out on purpose: the only clean CC0
football chant found named another school.

## Recordings

| File | Used as | Source | Author | License | SHA-256 |
|---|---|---|---|---|---|
| `crowd/college-football-crowd.mp3` | Stadium bed (looped) | [College Football Game.wav](https://freesound.org/s/328623/) | timmyg62 | CC0 1.0 | `532ddcf3a26904a772bd724a94305365ffadea63c48b99dc811500ee4f32340b` |
| `crowd/goal-reaction-roar.mp3` | Touchdown roar | [19 Football Crowd - Reaction To Goal.flac](https://freesound.org/s/528799/) | D.jones | CC0 1.0 | `06b918aa8986ac38bd5f189f0038b08f4194760f36801f5e588093cd842a6235` |
| `crowd/roaring-crowd.mp3` | Alternate roar | [RoaringCrowd.wav](https://freesound.org/s/130568/) | benfree | CC0 1.0 | `b75957f07b5702256004beb31a59a8dd762785a197e2133bbf8fcf7d92fc6714` |
| `crowd/crowd-cheer.mp3` | Cheer for a good play | [Crowd Cheer](https://freesound.org/s/397434/) | FoolBoyMedia | CC0 1.0 | `b621e391cb850ff79d8c215faf295119e747340803429ffbb383b5eaf4a132d2` |
| `crowd/crowd-cheer-2.mp3` | Away-fan pocket and pre-snap build | [Crowd Cheer II](https://freesound.org/s/397435/) | FoolBoyMedia | CC0 1.0 | `efe57f1e7dbed21e1561b8090cf54301a78775b4b8bf8ff418507c7e364f7f56` |
| `crowd/crowd-oh-disappointed.mp3` | Home groan | [crowd `oh - - disappointed.mp3](https://freesound.org/s/619007/) | mrrap4food | CC0 1.0 | `ab4324ebc6b6f96846df7da0ef6c15471207f353d0621d039a0e77e30d512357` |
| `crowd/sport-crowd-booing.mp3` | Boos for a flag on the home team | [JM_AMB_INT_Crowd Sport 01 - Booing.wav](https://freesound.org/s/557189/) | Julien_Matthey | CC0 1.0 | `69ab76b867bc676f569cafae66543d576f1b99ab6208749e15011681da1c2ac3` |

### Processing

Files are shipped unmodified. All processing happens at playback, from `manifest.json`:

- **Trim.** Each layer plays only its `start`–`end` region in seconds.
- **Loudness.** `gain_db` brings that region to −20 LUFS integrated (ITU-R BS.1770-4, measured by
  `tools/measure_loudness.mjs`), so every layer starts from the same reference before the mix.
- **Bed.** The bed region is looped with an equal-power crossfade at the seam.
- **Away pocket.** Band-limited (250 Hz–3.2 kHz), panned to one side and delayed slightly, so it sounds
  like a distant section of visiting fans.
- **Pre-snap build.** `crowd-cheer-2.mp3` faded in over about three seconds under a rising bed. This is a
  designed swell made from a real recording.
- **Mix.** Layers are mixed in the browser with ducking under the commentators (the bed by about −10 dB, reactions
  by about −4 dB) and a shared compressor. A master stage (make-up gain, limiter, soft clip) brings the finished
  broadcast to about −16 LUFS with peaks under −1 dBTP.

## Designed stingers

| File | Moment | Made by | SHA-256 |
|---|---|---|---|
| `stingers/intro.wav` | Show open | `tools/make_stingers.py` (synthesized) | `83823c05173d7ae00c319e8f8ce71e0a871528a2f7bf5870daec28f9c9974234` |
| `stingers/quarter.wav` | End of a quarter | `tools/make_stingers.py` (synthesized) | `118aa5bdc7bfa34d50551ae2276878349850a132d7e38b3627bec6b9c15157f0` |
| `stingers/halftime.wav` | Halftime | `tools/make_stingers.py` (synthesized) | `6eae657db5b447d2cf103bf88ffe30c0bf51800400f57187e4eaa955eb91ad71` |
| `stingers/final.wav` | Final whistle and outro | `tools/make_stingers.py` (synthesized) | `3d2f7342140ed7d639d514ca20474d4987e08c90b4daaaa0a7d51d5f2ce65cb8` |
| `stingers/score.wav` | Reserved for scoring bumpers | `tools/make_stingers.py` (synthesized) | `6a15a17f4133e218b8fe8966ea9dede45938c48c8742619f62f8dbcc8291912d` |

The stingers are reproducible: running the script again writes byte-identical files.

## Procedural fallback

If the browser cannot fetch or decode these files, the dashboard falls back to its earlier procedural crowd texture.
That texture is designed sound generated in `frontend/src/soundscape.ts`, and no file is involved.
