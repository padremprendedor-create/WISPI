# WISPI

**Local, offline, private voice dictation for Windows.** Hold `Ctrl+Win`, speak, release,
and the text appears wherever your cursor is. No network, no quota, no subscription, and
your audio never leaves the machine.

[![CI](https://github.com/padremprendedor-create/WISPI/actions/workflows/ci.yml/badge.svg)](https://github.com/padremprendedor-create/WISPI/actions/workflows/ci.yml) ![MIT license](https://img.shields.io/badge/license-MIT-blue) ![Windows 10/11](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D6) ![Python 3.11–3.12](https://img.shields.io/badge/Python-3.11–3.12-3776AB) ![100% offline](https://img.shields.io/badge/network-0%20calls-success)

*[Léeme en español →](README.md)*

```bash
uv run python -m wispi --console
```

> **Heads up on language.** WISPI ships configured for **Spanish** (`asr.language: es`),
> and the jargon dictionary, the filler-word rules and all in-code comments are Spanish
> too. The architecture is language-agnostic — see [Using another language](#using-another-language).

---

## Why it exists

WISPI was built to replace [Wispr Flow](https://wisprflow.ai), which is genuinely good
and fails in three specific ways:

| Problem | Evidence |
|---|---|
| All audio goes to the cloud, no offline mode | *"Transcription always occurs on the cloud"* — [wisprflow.ai/data-controls](https://wisprflow.ai/data-controls) |
| Breaks when dictating into Claude Code on Windows | [anthropics/claude-code#38620](https://github.com/anthropics/claude-code/issues/38620) |
| Doesn't know your jargon: mangles Supabase, Vercel, n8n, RLS, commit | Daily use |

Plus $15/month for Pro, or a 2,000 words/week ceiling on the free plan.

The real goal is written down in [SPEC.md](SPEC.md) (Spanish) and it isn't "build a
dictation app": it's **to make Wispr Flow uninstallable without missing it**. That's the
criterion that breaks ties.

It's published in case it's useful to someone else. It's a personal tool, not a product:
no installer, no autoupdate, no support. The code is complete and heavily commented,
which is the part actually worth sharing.

---

## How it works

```
Ctrl+Win ↓                                                    Ctrl+Win ↑
    │                                                              │
    │  WH_KEYBOARD_LL hook (callback < 0.5 ms)                     │
    ▼                                                              ▼
[300 ms pre-roll ring] ── record ──▶ [+200 ms tail] ──▶ RMS gate
                                                                │
                    discard if rms < 0.012 or dur < 0.35 s ◀────┤
                                                                ▼
                                                    faster-whisper small int8
                                                       (+ initial_prompt)
                                                                │
                                        hallucination filter ◀──┤
                                                                ▼
                                          level 0: fillers + dictionary
                                                                │
                            ┌───────── > 25 words? ─────────────┤
                            │ yes                            no │
                            ▼                                   ▼
                insert RAW text now  ─────────────────▶  insert and finish
                            │
                    Ollama in parallel
                            │
                  4 gates ── any fails ──▶ raw text stays
                            ▼
                          patch
```

**Cascading injection**, chosen per foreground app: `Ctrl+V` by default · `Shift+Insert`
in terminals (where `Ctrl+V` doesn't paste) · Unicode `SendInput` as a last resort.

---

## System requirements

### Required

| | Minimum | Recommended |
|---|---|---|
| **OS** | Windows 10 21H2 x64 | Windows 11 x64 |
| **CPU** | 4 physical cores with AVX2 | 6+ physical cores |
| **Free RAM** | 2 GB | 4 GB |
| **Disk** | ~750 MB | ~750 MB |
| **Microphone** | any | headset boom mic |
| **Python** | 3.11 | 3.11 or 3.12 (**not** 3.13) |

**Windows only, and not out of laziness.** The keyboard hook, text injection and
clipboard handling are pure Win32. There is no macOS or Linux build and none is planned.

**Your CPU determines the latency.** All ASR runs on CPU with int8 quantization; without
AVX2 throughput drops badly (any CPU from 2015 onward has it). Measured on a 10-core
i9-10850K: **p50 of 1.57 s per dictation**. Fewer cores means slower; `wispi.bench` tells
you exactly how much on your machine instead of making you guess.

**Measured footprint** with the `small` model loaded: 372 MB resident idle, 398 MB after
transcribing, **720 MB peak** during transcription.

**Disk**: 258 MB for the virtualenv + 464 MB for the `small` model. Other models:
`tiny` 75 MB · `base` 141 MB · `large-v3` 2.9 GB.

### Optional

- **Ollama** with `llama3.1:8b` (4.9 GB on disk, ~6 GB RAM while running) for level-1
  cleanup on long dictations. **WISPI works fine without it**: it stays at level 0 —
  rules and dictionary — which already does most of the work.
- **NVIDIA GPU** to move up to `large-v3`. Not needed, and not recommended up front (see
  [the migration ladder](#the-migration-ladder)).
- **`piper-tts`** and a Spanish voice, only if you want to regenerate the test corpus.

### Using another language

`asr.language: es`, the dictionary and the filler-word rules are Spanish. To switch, you
change `asr.language`, rewrite `dictionary.yaml`, and go through the rules in
`wispi/postprocess/level0.py`. Nothing in the architecture prevents it — nobody has done
it yet, and it's the contribution that would help the most.

---

## Installing from scratch

### 1. Install `uv`

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

`uv` handles Python too, so you don't need 3.11 installed beforehand.

### 2. Clone and set up

```bash
git clone https://github.com/padremprendedor-create/WISPI.git
cd WISPI
uv sync
```

This creates `.venv` (~258 MB) with the exact versions from `uv.lock`. They're pinned on
purpose: this stack breaks easily across minor versions.

### 3. Download the Whisper model

WISPI ships with `local_files_only: true`, meaning **it never touches the network**. For
the first download, pick one:

**Option A — fetch it yourself** (recommended: you see exactly what lands and from where):

```bash
uv run python -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-small')"
```

**Option B — let WISPI fetch it**: set `asr.local_files_only: false` in `config.yaml`,
start it once, then **set it back to `true`**.

It goes to `~/.cache/huggingface/hub`. Point `asr.download_root` elsewhere if you prefer.

### 4. (Optional) Ollama for level 1

```bash
ollama pull llama3.1:8b
```

If you skip it, set `llm.enabled: false` in `config.yaml` to avoid the connection attempt.
WISPI already logs a warning and keeps working when Ollama doesn't answer.

### 5. Check everything landed

```bash
uv run python -m wispi.selftest --all
```

It tells you, component by component, whether audio, the model, injection or the LLM is
broken. **Run it before opening an issue** — its output saves the most time.

---

## First run

```bash
uv run python -m wispi --console
```

Open Notepad, put the cursor in it, hold `Ctrl+Win`, say a sentence, release. Text should
appear in under two seconds. If nothing shows up, see [Troubleshooting](#troubleshooting).

### Start with Windows

```powershell
.\scripts\install_autostart.ps1              # unelevated (default)
.\scripts\install_autostart.ps1 -Elevated    # explicit opt-in
```

Registers a scheduled task that launches WISPI with `pythonw.exe`, no console window.
Remove it with `.\scripts\uninstall_autostart.ps1`.

`-Elevated` makes the hook work over administrator windows too (UIPI blocks input from
low-integrity processes toward high-integrity windows). **Read
[SECURITY.md](SECURITY.md) first**: a global keyboard hook running as admin has the same
technical shape as a keylogger, and the only real countermeasure is that the code doesn't
write down what it sees. That's why the default is unelevated.

---

## Usage

| Gesture | What it does |
|---|---|
| Hold `Ctrl+Win` | Records while held (push-to-talk) |
| Quick double-tap of `Ctrl+Win` | Hands-free: stops after 1.2 s of silence |
| `Esc` while recording | Cancels. Zero characters inserted |
| **Saying "hey WISPI"** | Hands-free without touching anything. Off by default — see below |
| **Single tap on the floating button** | Start or stop dictation |
| **Double tap on the button** | Opens the touch keyboard |
| Drag the button | Move it; position is saved automatically |

```bash
uv run python -m wispi                      # tray, no console
uv run python -m wispi --console            # console, for development
uv run python -m wispi.selftest --all       # component diagnostics
uv run python -m wispi.selftest --wake      # calibrate the wake word
uv run python -m wispi.bench --analyze      # is it time to change engines?
uv run python tools/e2e_pipeline.py         # 21 automated tests, no human voice
uv run python tools/test_wake.py            # 36 wake-word cases, no voice needed
```

### "hey WISPI" — dictating without touching anything

**Off by default.** Turn it on in `config.yaml` (or in the *Palabra clave* tab of the
settings window):

```yaml
wake:
  enabled: true
```

Say "hey WISPI", hear the chime, talk, and it stops when you go quiet. It does not
replace `Ctrl+Win` — that stays the fast path and the one that never fails — it covers
the case where your hands aren't on the keyboard.

**Why it doesn't burn CPU.** The obvious approach is running Whisper over a sliding
window every second, and that means fans forever: the encoder cost is *fixed*. WISPI
segments first and recognizes second, so the recognizer only ever sees a **short,
isolated utterance** — between 0.25 and 2 s of speech, closed by 350 ms of silence —
which is exactly the shape of saying "hey WISPI" and stopping. In a quiet room the
recognizer is **never called**; with a meeting, a phone call or the TV on, it isn't
either: that's continuous speech and it's discarded without looking at the content.
Check it yourself with `--wake` and watch the *analizados* counter.

**Why there's no new dependency.** Porcupine needs a Picovoice account; openWakeWord
ships no "hey wispi" and training one takes hours; Vosk only accepts words in its
lexicon and "wispi" isn't Spanish. There's already an engine loaded and a microphone
open here, so the detector uses a separate `tiny` (440 ms per candidate at 2 threads,
measured) and downloads nothing new.

**Matching is fuzzy on purpose.** "wispi" isn't a Spanish word and the model spells it
differently every time: *Wispy, Guispi, Vispi, wis pi*. Demanding an exact string would
mean demanding the model nail an invented word. It compares by similarity with two
thresholds, and the second one — the name alone — is what stops **"hey wifi"** from
counting. The 18 accepted variants and 18 rejected traps live in `tools/test_wake.py`;
run it if you touch a threshold.

**The wake phrase is never typed:** on waking, recording starts *without* pre-roll,
precisely so the tail of "hey WISPI" doesn't end up dictated.

And it **only listens at rest**. While recording, transcribing, polishing or paused the
detector is deaf: it can't hear itself and can't steal cores from the real engine.

### Touch keyboard

Double-tapping the button opens a panel with four tabs: **Keys** (Enter, Tab, Esc,
Backspace, Delete, Space — Backspace and Delete auto-repeat when held), **Move** (arrows,
Home/End, selection, copy/paste/cut, undo/redo), **Symbols** (36 symbols that are painful
to dictate: `@ # / \ | { } < > ¿ ¡ € …`) and **Options** (pause, status, settings, quit).

**Enter is what turns this into a keyboard replacement.** WISPI never injects a trailing
newline when dictating — so a prompt dictated into Claude Code doesn't send itself — which
means without this button you'd dictate a message and still need the physical keyboard to
send it.

**Deleting is just Backspace, no magic.** No "undo last dictation" guessing which
characters were ours. It deletes what's before the cursor exactly like the physical key.
Same mental model as a keyboard, zero surprises, and zero risk of WISPI wiping something
you typed yourself.

### About the `Ctrl+Win` combo

It's Wispr Flow's combo on purpose, so migrating needs no relearning. It's safe because
`Win` only opens the Start menu on release, and Windows won't open it while `Ctrl` is
held. WISPI **never** swallows those keys: it only observes.

> **Spanish keyboards:** never use `Right Alt` as a hotkey — it's AltGr and produces
> `@ # ~ [ ] \`. Same for `Ctrl+Alt+<key>`, which Windows treats as AltGr+key.

---

## Configuration

`config.yaml` hot-reloads (< 3 s, no restart). So does `dictionary.yaml`. The settings
window writes to `config.local.yaml`, which **overrides** `config.yaml` — so if you edit
a key by hand and see no effect, look there.

**Both files are commented in Spanish.** The keys are English; the explanations aren't.

```yaml
asr:
  model: small              # tiny | base | small | large-v3
  cpu_threads: 10           # SET THIS TO YOUR PHYSICAL CORE COUNT
audio:
  silence_threshold: 0.012  # raise it if your room is noisy and empty dictations slip in
llm:
  enabled: true             # false if you have no Ollama
  min_words: 25             # below this the LLM is not called
```

### Your own jargon is half the value

`dictionary.yaml` ships 40 general technical terms (Supabase, commit, n8n, RLS...) with
305 variants of how Spanish Whisper mangles them. It attacks the problem from both ends:
canonical forms feed the `initial_prompt` (**preventing** the error inside the decoder)
and variants compile into one regex (**curing** whatever slips through).

What it does *not* ship — because nobody else says them — are the proper nouns of your
projects, clients and teammates, which are exactly the ones Whisper gets worst, since
they were never in its training corpus. Adding them takes ten minutes and is where the
payoff is highest: **[`dictionary.personal.example.yaml`](dictionary.personal.example.yaml)**
has the format, the method for deriving variants by measuring instead of guessing, and
the traps.

The main rule, documented in the file itself: **no variant may be an ordinary word of
your language**. A false positive (eating a good word) is far worse than a false negative
(leaving a "comit" unfixed), because you don't see it coming.

---

## The measurement that picked the model

Before a line was written, the ASR was measured (i9-10850K, int8, `cpu_threads=10`,
`beam_size=1`):

| model | floor per dictation |
|---|---|
| `large-v3` | **6.26 s** |
| `small` | **1.24 s** |
| `base` | 0.36 s |
| `tiny` | 0.21 s |

**The cost is fixed, not proportional to the audio**: a 5 s clip and a 10 s clip give the
same number. Whisper pads to a 30 s window and runs **one encoder pass** regardless. For
short dictations — all WISPI ever does — the encoder *is* the latency.

Two verified corollaries, not opinions:

- **`large-v3-turbo` and `distil-large-v3` don't help.** They trim the *decoder* (32→4
  layers) and keep the large-v3 encoder intact. 1.6 GB of download to stay at ~6.3 s.
- **`chunk_length` is a dead knob.** Tested `None`/15/10 → 6.39 / 6.53 / 6.43 s.

Hence `small` as the default, with the quality gap closed where it actually belongs: the
dictionary feeding `initial_prompt` **prevents** the error inside the decoder instead of
patching it afterwards.

### The migration ladder

`large-v3` isn't abandoned — but the measurement shows its only path is the GPU. When and
how to climb that rung is decided by `wispi.bench`, not by a hunch:

```bash
uv run python -m wispi.bench --analyze
```

Target: **p50 ≤ 1200 ms, p90 ≤ 2000 ms** over ≥ 100 real dictations across ≥ 3 days. If
it holds, migrate nothing. If it doesn't, first check whether `asr_ms / ttt_ms > 0.6`:
below that the bottleneck **isn't the model** and swapping it fixes nothing.

If you do go GPU: `uv sync --extra gpu`, **in a separate venv** (`.venv-gpu`). A
half-finished CUDA install loads the model and then explodes on the first transcription —
better that it doesn't take your working environment with it.

## Prompt format matters more than prompt content

The plan assumed that putting jargon in `initial_prompt` is pure prevention and therefore
better than regex. **That's only true if the prompt is prose.**

Whisper treats `initial_prompt` as preceding text and **continues its style**, not just
its vocabulary. Given a list like `"Supabase, Vercel, Next.js, ..."` the decoder starts
returning lists and truncates: a 13-word sentence came out as
`"workflow, n8n, webhook, prisman,"`.

| prompt format | retention | jargon | p50 |
|---|---|---|---|
| no prompt | 99 % | 17/31 | 1494 ms |
| **list** | **77 %** | 27/31 | 1566 ms |
| **prose** (chosen) | **100 %** | **30/31** | **1534 ms** |
| prose + `beam=5` | 100 % | 31/31 | 1893 ms |

Prose wins on both axes at once and costs 40 ms over having no prompt at all. `beam=5`
buys one more term for +359 ms: rejected.

The prose lives in `dictionary.yaml::prompt_prose` and is hand-written. Delete it and one
is generated from the canonical forms — works, but worse. And `initial_prompt_style` is
**deliberately empty**: a meta sentence like *"Technical transcription in Spanish with
English terms"* isn't natural language and it bleeds through — with it set, *"Crea un
endpoint"* came out as *"Create an endpoint"*.

> If you ever change the prompt, run `uv run python tools/e2e_pipeline.py` before calling
> it good. This failure is invisible when reading the code; you only see it by measuring.

---

## Privacy

- **Nothing leaves the machine.** Local ASR, local LLM (Ollama), zero API keys, zero
  telemetry, zero accounts.
- `logs/latency.jsonl` stores timings and lengths, **never the transcribed text**
  (`logging.include_text: false`).
- The logger never records `vkCode` for keys outside the configured combo.
- With `local_files_only: true`, WISPI starts and transcribes with no network at all.
- **The wake word ships off.** It's the only feature that analyses the microphone
  without you asking for anything, so switching it on has to be your decision, not a
  surprise. With it on, still not one byte leaves the machine — recognition is local,
  same as dictation — and **what it hears is never written to any log**, not even what
  it discards, unless you deliberately set `include_text: true`.

WISPI installs a global keyboard hook, reads the clipboard and injects keystrokes. That
deserves more than a paragraph: the full threat model, what to verify and in which file,
is in **[SECURITY.md](SECURITY.md)** (Spanish).

---

## Known limitations

- **Windows only.** No porting planned.
- **Spanish out of the box.** See [Using another language](#using-another-language).
- **No streaming.** You don't see words as you speak; the text lands all at once on
  release. That follows from the encoder running once over the whole clip.
- **If injection fails in an unusual app, the text is lost.** There's no dictation history
  yet (it's on the [roadmap](ROADMAP.md)).
- **`Ctrl+V` uses the clipboard.** It's restored afterwards, but for ~400 ms the clipboard
  holds your dictation.
- **No installer, no code signing.** You install it by cloning the repo. Your antivirus
  will likely ask questions; see [SECURITY.md](SECURITY.md).
- **Thoroughly tested on one machine, with one voice.** Criteria requiring real speech,
  real apps or a reboot are marked 🔴 in [SPEC.md](SPEC.md): those are honest statements
  of what hasn't been verified, not oversights.

---

## Troubleshooting

**Nothing appears when I dictate.** Run `uv run python -m wispi.selftest --all` and see
which part fails. Most common: wrong mic in `audio.input_device`, or speaking too quietly
for `audio.silence_threshold`.

**Works in Notepad but not in my terminal.** `Ctrl+V` doesn't paste in many terminals. Add
the `.exe` to `injection.terminal_apps` so it uses `Shift+Insert`.

**Doesn't work over an administrator window.** That's UIPI, not a bug. You need the
elevated autostart — read [SECURITY.md](SECURITY.md) first.

**It eats the first syllable.** Raise `audio.preroll_ms`, and check `audio.start_grace_s`.

**It discards good dictations as silence.** Lower `audio.silence_threshold` (default
0.012). If the opposite happens and empty dictations slip through, raise it.

**Model won't load / `local_files_only` error.** You haven't downloaded the model yet. See
[step 3](#3-download-the-whisper-model).

**It suddenly stopped responding to the hotkey.** Windows enforces `LowLevelHooksTimeout`
and silently unhooks slow hooks. A watchdog reinstalls every 5 minutes; if it happens
often that's a bug — open an issue with the selftest output.

---

## Layout

```
wispi/
  app.py           state machine — the ONLY place with orchestration logic
  winapi.py        ALL ctypes declarations (Win64 bugs are type bugs)
  hotkey.py        WH_KEYBOARD_LL hook + watchdog          ← failure point #1
  audio.py         permanent InputStream + pre-roll ring
  wake.py          "hey WISPI": segments first, recognizes only then
  asr/base.py      the Protocol that makes the engine swappable
  inject/          Ctrl+V / Shift+Insert / Unicode cascade ← failure point #2
  postprocess/     level 0 (rules + dictionary) and level 1 (Ollama)
  metrics.py       instrumentation → logs/latency.jsonl
  bench.py         the migration decision rule
tools/
  make_corpus.py   generates the corpus with Piper (no human voice needed)
  target_window.py target window for testing cross-process injection
  e2e_pipeline.py  the 21 end-to-end tests
  test_wake.py     36 wake-word cases (matching + segmentation)
```

## The two places this can break

1. **`hotkey.py`.** Windows enforces `LowLevelHooksTimeout` (300 ms) and if the callback
   exceeds it, **it unhooks silently, with no warning**. In Python the real risk isn't
   being slow, it's not being able to *start* because another thread holds the GIL. Hence
   the < 0.5 ms callback that only does `put_nowait`, the `setswitchinterval(0.001)`, and
   the watchdog that blindly reinstalls every 5 minutes.

2. **`inject/`.** Text injection is fragile by design on Windows. The Wispr Flow incident
   with Claude Code shows funding doesn't fully solve it. That's why the cascade existed
   from day 1 rather than as a patch, and why the optimistic patch has **four gates**:
   same HWND and PID, < 1.5 s elapsed, zero user keystrokes since insertion, and the app
   not in `no_patch_apps`. Any gate fails and the raw text stays. Losing the cleanup is
   annoying; corrupting a document is unacceptable.

---

## Status and contributing

[SPEC.md](SPEC.md) has the verifiable success criteria and which ones are still open.
[ROADMAP.md](ROADMAP.md) has what's done and what's next. Both are in Spanish.

The most useful contribution isn't a PR: it's **telling us what failed, in which app, on
which machine**. WISPI was tested thoroughly on a single machine with a single voice. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE). Do whatever you want with it.
