## My answers (edit before sending)
1. The voice I listen with: [Azure "Use expressive voices" (GPT-Live)] / [Demo browser voice] / [both]
2. "Interactive" means: (a) the two hosts really react to and talk over each other like a real booth, (b) I, the listener, can talk to or influence the broadcast, (c) both. → [c]
3. Can you add a new paid voice provider (ElevenLabs, Cartesia, Hume, Google, etc.)? → [Research and recommend, implement on Azure first, ask me before adding any new API key]
4. Can you add free/CC0 stadium recordings and short music stingers to the repo? → [Yes, with a license file]
5. Can the hosts talk more per play (longer lines, chat between plays) even if it costs more? → [Yes, tell me the cost per game]

## Goal
Make BanterBots sound like a real, entertaining NFL broadcast that people want to keep listening to, not two AIs reading lines. Success is judged by listening, not by tests passing.

## What I hear now
I can hear both voices, but it sounds like AI: flat, polite, predictable. The hosts don't really interact. There are long silences. The crowd sounds like noise, not a stadium. Nothing makes me excited or makes me laugh.

## Causes already found in the code (confirm them, don't start from zero)
1. Write-then-read pipeline: the text model writes a line, then a fresh GPT-Live session is told to "Read validated_call exactly once" (backend/speech.py, voice_session and the instructions.append call). A live conversational voice model is being used as plain text-to-speech.
2. Energy is held down on purpose: backend/delivery.py starts ordinary plays at about 0.30–0.38 energy and tells the voice "Energy N percent of your broadcast range". Replies lose another 0.16. The personas and build_prompt in backend/commentary.py keep repeating "ordinary snaps breathe", "at most one earned exclamation", "don't force a joke" and "one peak, not constant shouting". Most plays are ordinary, so most of the broadcast is flat.
3. Too little talk: max_words is 28 for Max and 24 for Riley, with exactly one call plus one reply per play. Nothing is said before the snap or between plays.
4. Slow handoffs: the reply is drafted only after the lead's voice session closes (needs ≥1 s of silence plus 1.2 s with no transcript), then a new text call (~2 s), then a fresh voice session starts. Real booths hand off in roughly 0–200 ms. The wait exists so the reply only answers words that were actually spoken. Keep that guarantee if you can, or explain the trade-off.
5. Dead air between plays: in backend/engine.py the replay loop waits for playback, sleeps max(0.5, 4/speed) seconds, and only then starts generating the next play.
6. Synthetic crowd: frontend/src/soundscape.ts builds the crowd from filtered noise plus 8 sine "voices", limited to 160–1800 Hz, at gain 0.11 (0.045 under speech). It has only 3 cue types, at most one every 3 s. There's no home/away difference, no boos, chants, PA, band, intro music or stingers.
7. Demo mode uses the OS browser speech engine, which will always sound robotic.
8. The listener can't influence anything.

docs/BROADCAST_RESEARCH.md is the earlier pass that produced this restrained design. Use it as a starting point, but its "stay restrained" conclusions are exactly what I'm pushing back on.

## Phase 1: Deep web research
Use real, current sources. Cover:
A. How real broadcasts entertain: play-by-play vs. color analyst roles; building anticipation before the snap and exploding on the result; how famous radio calls are structured; banter/rivalry formats (ManningCast-style, two-homer broadcasts, sports podcasts); pacing across a game (opening, two-minute drill, quarter/halftime recaps, final call). Learn the structure only. Don't copy calls or imitate real broadcasters.
B. Why synthetic voices sound like AI (flat prosody, no laughter or fillers, no backchannels or overlap, identical rhythm, perfectly even gaps) and what makes conversational/podcast-style AI audio sound human.
C. Voice technology available today, with latency, price and expressiveness: how far GPT-Live can be pushed (one persistent session per host, free performance vs. exact script, its control events); Azure Speech HD voices with sports styles/SSML; ElevenLabs (audio tags, multi-speaker dialogue), Cartesia, Hume, Google multi-speaker TTS and others. Which can do laughter, shouting, interruptions, or two speakers in one generation?
D. Stadium sound design: layered crowd recordings (bed, murmur, swell, roar, groan, boos, chants), home vs. away crowd, crowd that builds on third down and in the red zone, licensed/CC0 sources and their terms, music stingers and transitions, ducking, and loudness targets for streaming.
E. Listener interaction in sports audio products: polls, cheer/boo reactions that move the crowd, "pick your side", push-to-talk questions, watch-party features.

Put the findings in docs/BROADCAST_RESEARCH.md with a source link for each, plus a ranked list of changes by entertainment impact versus effort, with cost and latency notes.

## Phase 2: Short plan, then check in
Show me the top changes, anything that needs a new account, API key or money, and the expected cost per game. Implement anything that needs no new provider right away. Stop and ask only before adding a new paid provider or key.

## Phase 3: Implement (adjust to what the research shows)
1. Writing: real play-by-play and color roles. In replay mode, add a pre-snap setup line from nflverse's pre-snap down/distance (ESPN live data is post-play, so not there), then the result call. Use a wider range: routine plays short and quick, big moments longer with a real burst. Give the hosts stronger opinions, running jokes, real callbacks, and arguments that carry across plays. Add short stats or drive summaries between plays, plus quarter, halftime and final-whistle segments. Include spoken reactions and laughter where the voice model supports them.
2. Voice performance: let the voice model perform the line instead of reciting it (facts stay locked; natural reactions allowed). Widen the energy range so touchdowns actually explode. Test one persistent session per host to cut startup time.
3. Turn-taking: aim for a handoff gap under 300 ms. Allow occasional short overlaps or interjections on big plays. Start generating the next play during playback so there's no dead air.
4. Sound: replace or layer the procedural crowd with real licensed/CC0 recordings. Home vs. away reactions, a crowd that builds in key moments, an intro/outro and short stingers for scores and quarter breaks, proper ducking, consistent loudness.
5. Listener interaction (per answer 2): e.g. pick your side, cheer/boo buttons that feed the crowd, quick polls before 3rd/4th down that the hosts react to, push-to-talk "ask the booth" answered at the next break using game facts only.
6. Demo mode: improve it as far as possible, or label it clearly as a preview and point people to the expressive voices.

## Rules to keep
- Every fact still comes from the frozen snapshot. Keep validate_draft and the causality rules: no future results, and reversed plays never get a celebration.
- Original voices and personas only. No imitation of real broadcasters, family-friendly, no betting talk.
- Record the license of every audio file in the repo. Label designed sound as designed.
- Don't break existing behavior: pause, mute, restart and game switching still stop all sound; recovered history stays silent; the Windows launcher still works.
- Keep pytest, ruff, the frontend check/build and Playwright passing, and add tests for new logic.

## How to prove it's better
- Record before/after audio of the same plays (a quiet drive, a long gain, a third-down conversion, a touchdown for each side, a turnover, the reversed Baltimore touchdown, the end of the game) into .runtime/ and tell me where they are.
- Report numbers: gap between hosts, silence between plays, words per minute, share of lines with a real reaction or callback, cost per game.
- Give me a 2-minute listening checklist so I can judge it myself. Passing tests isn't the goal; a broadcast I'd want to keep listening to is.
