# Broadcast research: from polite to entertaining

Research pass: October 10, 2026. It replaces the October 9 "audience-first audio direction" pass, which produced a deliberately restrained booth. Several of that pass's findings still hold and are kept below. Its conclusions do not: "ordinary snaps breathe", "at most one earned exclamation", replies 0.16 quieter than the call, and a whisper-level crowd. Listening showed that most plays are ordinary, so a booth that saves its energy for rare moments is flat most of the time.

Sources were opened in a web reader on the date above. Prices and model capabilities change quickly; re-check them before budgeting. No real broadcaster's calls, catchphrases or voices are copied. Only structure is borrowed.

## A. How real broadcasts entertain

| Finding | Source | What BanterBots does with it |
|---|---|---|
| Play-by-play describes the action as it happens. Color adds analysis, stories and statistics, usually in the gaps. | [JEA: color commentator vs play-by-play](https://jea.org/digital-media/whats-the-difference-between-a-color-commentator-and-a-play-by-play-announcer), [Wikipedia: Sports commentator](https://en.wikipedia.org/wiki/Sports_commentator) | One host owns the snap and the other owns the "why" and the opinion. The two jobs never repeat the same summary. |
| Radio play-by-play has to say more than TV because the listener has no picture. | [Wikipedia: Sports commentator](https://en.wikipedia.org/wiki/Sports_commentator) | Routine calls are short but complete: who, what, how far, and what's next. |
| Set up *every* snap with down, distance and yard line, because any play can become a big play. Repeat time and score every two to three minutes for listeners who just tuned in. | [STAA: 4 tips for football play-by-play](https://staatalent.com/4-tips-football-play-by-play/), [Sportscaster Life: habits of successful announcers](https://www.sportscasterlife.com/habits-of-highly-successful-announcers/) | A pre-snap setup line from nflverse's *pre-snap* down, distance and yard line, generated without the result. The score and clock come back regularly. |
| Excited radio commentary rises in suspense, and the climax slows and lengthens the stressed syllables. Background information is calmer. | [Kern, *Speaking dramatically*](https://www.benjamins.com/catalog/sidag.23.18ker), [Trouvain & Barry 2000](https://www.isca-archive.org/speechemotion_2000/trouvain00_speechemotion.html), [Samlowski et al. 2018](https://www.isca-archive.org/speechprosody_2018/samlowski18_speechprosody.pdf) | Build in the setup, explode on the result, then release. This keeps the earlier arc but moves the floor up: routine plays are engaged, not hushed. |
| ManningCast works because it feels like watching with friends: the hosts interrupt, digress and needle each other, and their personalities carry the show. Its weakness is that digressions can bury the game. | [Boston.com](https://www.boston.com/sports/media/2021/09/18/we-regret-to-inform-patriots-fans-peyton-eli-manningcast-works/), [Barrett Media: what makes an alternate broadcast great](https://barrettmedia.com/2023/06/29/alternate-broadcast-great/), [Awful Announcing](https://awfulannouncing.com/nbc/super-bowl-lx-coverage-no-altcast-good-thing.html) | Two original homers with strong opinions, running bits and callbacks. The snap still comes first, so banter lives in the reply, between plays and in segments. |
| Broadcasts follow a game arc with distinct registers: opening, quarter breaks, halftime recap, two-minute urgency and a final call. | Practitioner guidance above. The 2-minute "late tension" rule already existed in `delivery.py`. | Quarter, halftime and final-whistle segments; drive summaries when a possession ends; extra urgency late in close games. |

## B. Why synthetic hosts sound like AI, and what fixes it

| Finding | Source | Application |
|---|---|---|
| NotebookLM's two-host audio adds a dedicated "disfluency" stage of fillers, micro-interjections ("Oh really?", "Totally") and pauses. The team says these come from the audio model, not the script. | [Simon Willison on NotebookLM](https://simonwillison.net/2024/Sep/29/notebooklm-audio-overview/), [Latent Space interview](https://latent.space/p/notebooklm) | Let GPT-Live *perform* the line, with natural reactions, laughter and a repeated word, instead of reading a script verbatim. |
| Filled pauses increase perceived authenticity of synthetic conversational speech, with a trade-off against fluency. | [Székely et al., Interspeech 2019](https://www.speech.kth.se/tts-demos/is19/szekely2019spontaneous.pdf) | Allow a few fillers and reactions. Never let them carry facts. |
| "Voice presence" depends on conversational dynamics (timing, pauses, interruptions, emphasis) and on contextual expressivity. | [Sesame: Crossing the uncanny valley of conversational voice](https://sesame.com/research) | Short handoffs, occasional overlaps on big plays, and energy that tracks the moment and each host's allegiance. |
| Human turn transitions cluster at 0–200 ms. | [Stivers et al., PNAS 2009](https://pmc.ncbi.nlm.nih.gov/articles/PMC2705608/) | Target under 300 ms between hosts. The measured "before" gap was about 10 s. |
| Perfectly even gaps, identical rhythm, no laughter, polite agreement and repeated summaries are the classic AI tells. Before this pass, BanterBots did all of them: 1 s+ silent tails, a reply that restated the call, and capped energy. | Code review: `speech.py`, `delivery.py`, `commentary.py` | Trim tails, vary line length by moment, make the partner disagree or needle, and add callbacks. |

## C. Voice technology (October 2026)

| Engine | Expressiveness | Two speakers / overlap | Latency | Price | Notes |
|---|---|---|---|---|---|
| **GPT-Live 1** (in use, via Azure) | Steered by natural-language instructions. Full duplex. There are no inline tags, so laughter and shouting must be directed. | One voice per session. Overlap is possible across two sessions. | Vendor-reported turn latency about 0.8 s | **$0.05 per minute of open session**, billed per second (OpenAI list; Azure rate unpublished) | `session.instructions.append` can interrupt and request exact wording. `session.commentary.append` is speakable and "may paraphrase". `session.thinking.append` is silent context. 128k context, 16k startup instructions, 500 tokens per append, no output-done event. |
| Azure Speech Dragon HD / HD Omni | Styles include `shouting`, `excited`, `laughing`, `amazed`, `disappointed`, plus `laughter` and `sighing` paralinguistics, as SSML or inline `[tags]` | MultiTalker Ava–Andrew voice (preview) | Under 300 ms | Neural HD per 1M characters | Needs a separate Speech resource key. `<prosody>` is not supported on HD voices. |
| ElevenLabs v4 / v3, Text to Dialogue | Audio tags such as `[laughs]`, `[jumping in]`, `[whispering]`; cut-offs written as "seat-" | Multi-speaker dialogue in one request (≤2,000 characters recommended) | Dialogue is not streamed | v4 $0.022 per 1k characters (promo until Oct 12), v3 $0.08 | Strongest at scripted two-host comedy. Needs an ElevenLabs key and a paid plan for commercial use. |
| Gemini 3.x Flash TTS | Inline tags (`[laughs]`, `[sigh]`) | Two speakers per request | Preview | About $0.037 per minute (3.1, third-party figure) | Preview models with no SLA. |
| Cartesia Sonic 3.x | `[laughter]`, emotion, speed and volume controls | No | 90 ms vendor, 166–190 ms measured | About $49 per 1M characters | Lowest latency. |
| Hume Octave 2 | Natural-language acting instructions | No | About 200 ms | $0.05–0.15 per 1k characters | Preview. |

Sources: [GPT-Live sessions](https://developers.openai.com/api/docs/guides/live-conversations), [GPT-Live server controls](https://developers.openai.com/api/docs/guides/voice-server-controls), [gpt-live-1 model page](https://developers.openai.com/api/docs/models/gpt-live-1), [voice latency and cost guide](https://developers.openai.com/api/docs/guides/voice-latency-cost), [third-party GPT-Live latency report](https://pasqualepillitteri.it/en/news/15720/openai-gpt-live-1-api-voice), [Azure HD voices](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/high-definition-voices), [ElevenLabs Text to Dialogue](https://elevenlabs.io/docs/overview/capabilities/text-to-dialogue), [ElevenLabs API pricing](https://elevenlabs.io/pricing/api), [Gemini TTS guide](https://dev.to/mr_manushukla/gemini-31-flash-tts-build-guide-30-voices-audio-tags-and-real-2026-pricing-59pf), [Cartesia Sonic overview](https://invideo.io/blog/cartesia-sonic-ai-voice/), [Hume TTS FAQ](https://dev.hume.ai/docs/text-to-speech-tts/faq), and [InstructTTSEval](https://arxiv.org/pdf/2506.16381), which found that few instruction-following TTS systems could produce laughter.

**Decision for this pass: GPT-Live only.** It is already paid for, it is the only option that can hold a whole game's booth context in one session, and `commentary.append` is designed for "say this, in your own way." The cost is that it has no tags, so whether it really shouts or laughs has to be tested by listening (see `docs/VALIDATION.md`). If it can't, the next experiment is Azure Speech HD Omni for its `[shouting]` and `[laughing]` styles, or ElevenLabs v4 dialogue for scripted overlaps. Both need a new key and your approval.

## D. Stadium sound design

| Finding | Source | Application |
|---|---|---|
| In 2020, NFL Films built crowd audio for every team from that team's own fans. A local operator rode two faders and a few buttons (cheer, boo) during the game, and the reactions were heard on the broadcast. | [NBC: inside the NFL's fake crowd noise system](https://www.nbcwashington.com/news/sports/the-invisible-crowd-inside-the-nfls-fake-crowd-noise-system/2447545/), [Patriots.com: sounds of the season](https://www.patriots.com/news/sounds-of-the-season-how-2020-s-artificial-crowd-noise-became-reality) | Layered recordings driven by play state: a home-team point of view, a build before 3rd down and in the red zone, a roar, groan or boo on the result, and an away-fan pocket. |
| Atmosphere establishes place, specific sounds add texture, and sound works as punctuation or a transition. | [BBC Academy: using sound](https://downloads.bbc.co.uk/academy/academyfiles/RadioPackages/Using%20sound%20in%20your%20radio%20package.pdf) | Crowd bed for place, reactions for punctuation, and short stingers for scores and breaks. |
| Keep voice levels matched, duck ambience under speech, and check that every word is clear at low volume. | [NPR mixing checklist](https://npr.brightspotcdn.com/dd/e4/4c73d7c74227a94967e00362b73a/npr-training-audio-mixing-checklist.pdf) | Duck the bed hard under voice but a roar only lightly, so a touchdown call rides on a loud crowd. |
| Podcast and streaming targets are about −16 LUFS integrated (Apple), or −14 (Spotify), with true peak ≤ −1 dBTP. Use measured loudness, not peaks. | [WBUR: LUFS for public radio, Spotify and Google](https://www.wbur.org/citrus/2020/07/02/lufs-loudness-public-radio-npr-spotify-google), [EBU loudness](https://tech.ebu.ch/loudness/) | Normalize every asset to a common reference at runtime and measure the recorded mixes. |
| Freesound marks each sound's license individually. CC0 candidates for crowd layers include: "RoaringCrowd" (benfree, 130568), "Crowd Cheer" I/II (FoolBoyMedia, 397434/397435), "College Football Game" (timmyg62, 328623), "Football Crowd – Reaction To Goal" (D.jones, 528799), "crowd booing" (HowardV, 264378), "Crowd Sport 01 – Booing" (Julien_Matthey, 557189), "AF Crowd Boo Hiss" (mglennsound, 678537–678540), "crowd oh disappointed" (mrrap4food, 619007) and "OHIO Chant" (spazo25, 130994). | [Freesound CC0 search: stadium crowd](https://freesound.org/search/?q=stadium+crowd&f=license%3A%22Creative+Commons+0%22), [crowd boo](https://freesound.org/search/?q=crowd+boo&f=license%3A%22Creative+Commons+0%22) | Only sounds whose own page says CC0 are shipped. Each one is recorded in `frontend/public/audio/LICENSES.md`. BBC Sound Effects (RemArc, non-commercial) and Pixabay/ZapSplat (redistribution limits) are not bundled. Stingers are *designed* in code and labelled as such. |
| Web Audio offers scheduled gain, filters and a compressor, but no sidechain input. | [Web Audio API](https://www.w3.org/TR/webaudio/#DynamicsCompressorNode) | Ducking is explicit gain automation driven by playback start and end. |

## E. Listener interaction

| Finding | Source | Application |
|---|---|---|
| Prime Video TNF layers alternate audio, X-Ray stats, AI "Insights", Rapid Recap for late joiners, and Twitch co-streams on the live game. | [Barrett Media, Sept 2026](https://barrettmedia.com/2026/09/15/prime-video-interactive-broadcast-enhancements-nfl/), [Awful Announcing: Prime Vision features](https://amp.awfulannouncing.com/amazon/thursday-night-football-prime-vision-new-features.html) | An optional listener panel that never interrupts the game. |
| Watch-with-friends formats (ManningCast, co-streams) succeed because the audience feels present in the room. | Section A sources | The hosts sometimes acknowledge the listener's side, poll vote and cheers. |
| The 2020 crowd operator shows that buttons moving a crowd is a real broadcast practice. | Section D sources | Cheer and boo buttons play immediately in "your section" and are counted, and the hosts react at the next break. |

Polls are predictions with no stakes ("Do they convert?"). There is no odds, wagering or betting language, matching the existing persona rules. Typed or spoken "ask the booth" questions are answered at the next break from game facts only. Listener text is data, never instructions.

## Ranked changes: entertainment impact versus effort

| # | Change | Impact | Effort | Latency effect | Cost effect |
|---|---|---|---|---|---|
| 1 | Remove dead air: trim the ≥1 s silent tail played after every line, pipeline the next play during playback, drop the `4/speed` sleep | Very high | Low–medium | −5 to −15 s per play | $0, and less open-session time |
| 2 | Real roles and range: pre-snap setup, short routine calls, long loud big plays, opinions, running bits, callbacks, disagreement | Very high | Medium | Setup hides the next draft's latency | Text stays under $0.30 per game |
| 3 | Let GPT-Live perform rather than recite, with a wider energy range and vivid acting direction | High | Low | None | $0 |
| 4 | Real CC0 crowd layers, home/away point of view, builds on key downs, designed stingers, loudness-matched mix | High | Medium | None | $0 |
| 5 | Fast handoffs: draft the reply while the call is spoken, warm or persistent voice sessions, overlaps on big plays | High | Medium–high | Handoff from about 10 s toward under 300 ms | Persistent sessions bill idle time |
| 6 | Between-play segments: drive summaries, quarter, halftime, final whistle | Medium–high | Medium | Fills breaks | About +$0.40 voice per game |
| 7 | Listener panel: pick a side, cheer/boo, 3rd/4th-down polls, ask the booth | Medium–high | Medium–high | Questions wait for a break | Small |
| 8 | Demo: prefer "Natural" browser voices, label it as a preview | Medium | Low | None | $0 |
| 9 | Second expressive engine (Azure HD Omni or ElevenLabs v4) | Possibly high | Medium | Varies | New key, so ask first |

## Cost per game

Measured before-cost and after-cost come from GPT-Live `usage.seconds` and text-model tokens recorded by `tools/listen_compare.mjs`; see `docs/VALIDATION.md`. Model at list prices: GPT-Live $0.05 per open-session minute, and `gpt-6-luna` $0.10 input, $0.01 cached input and $0.50 output per 1M tokens (third-party listing of OpenAI's price). Text costs a few cents per game. Voice dominates, and depends on how long sessions stay open:

- **Per-turn sessions** (before): each line opens a session for startup, speech and the silence tail.
- **Persistent sessions**: two sessions billed for the whole game, silence included. That is cheaper per line but expensive when idle, so sessions close after an idle period. In live mode, a three-hour game with two always-open sessions would cost about $18.

## What changed from the October 9 conclusions

Kept: an arc (setup, burst, release), corrected rulings that are never celebrated, sound that never carries facts, ducking under speech, honest labels for designed sound, and measuring separately rather than assuming.

Retired: restraint as the default, the 0.30–0.38 energy floor, replies 0.16 quieter, "at most one earned exclamation", "don't force a joke", a one-sentence limit for routine plays, and designed noise as the whole crowd.

## Trade-off: fast handoffs versus "only answer what was spoken"

Before, the partner's reply was written only after the lead's voice session closed. That guaranteed the reply answered the actual spoken words, but it cost about 10 s per play. Now the partner drafts its reply over A2A *while* the lead is speaking, from the lead's validated script. When the lead's observed transcript is sealed, the draft is used only if the observed words contain every name and number the script contained and add no checked fact (numbers, scores, touchdowns, down and distance) that fails validation. Otherwise the reply is regenerated from the observed words, as before. The guarantee is now "the reply only answers facts that were actually spoken," not "the reply waited for the last breath."

The handoff now goes one step further. The partner starts speaking once every name, team and number in the lead's line has been heard, nothing has been misspoken, and at most four script words remain. Its first audio (1.3–2.3 s on a warm GPT-Live session) then lands as the lead finishes. If the lead's speech then fails or misspeaks, the early reply is withdrawn before it is heard.

## GPT-Live audition (October 10)

Run with `tools/voice_audition.py` on the configured Azure resource. Each run spoke five lines: routine, own touchdown, rival touchdown, needling reply, home touchdown. Reports are in `.runtime/listening/audition-*/report.json`, with WAVs alongside.

| | First audition (old cue) | Tightened cue and ad-lib cut (`persistent`) | `warm` sessions |
|---|---|---|---|
| Lines spoken exactly as written | 2 of 5 | 5 of 5 | 4 of 4 (one blocked by moderation) |
| Invented facts | 1 ("He just blew past him") | 0 | 0 (one trailing "But" cut) |
| Routine line length | 9.2 s | 2.0–2.4 s | 2.0 s |
| Cue to first audio after warm-up | 1.2–3.4 s | 1.3–2.3 s | 2.8–4.5 s |
| Caption time vs. first audible sample | not measured | captions 100 ms early (cut lands correctly) | — |

Findings:

- **GPT-Live does perform on instruction.** It stretched "Touchdoooown Chiefs!", added `[laugh]` and "Ha!" on the home touchdown, and shouted the explode lines about 1–2 dB louder (RMS) than routine lines.
- **The groan contour is quieter but not flat.**
- **Laughter is kept;** sentence-length ad-libs are cut.
- **`persistent` stays the default.** It answered fastest.
- **A false moderation stop is retried once** as a plain read, so a line is not lost.

Whether the energy is *enough* is a listening judgment. If the recorded scenarios still sound too polite, the next experiment is the second expressive engine in row 9. That needs a new key, so ask first.
