<div align="center">

# ⚙️ JARVIS — MARK LX (60)

### 🤖 Cross-Platform Personal AI Assistant & Universal Learning Station

**Hear • See • Speak • Study • Control your computer — on any OS**

![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-blue?style=for-the-badge)
![Gemini Live](https://img.shields.io/badge/Powered%20by-Gemini%20Live%20API-8E75B2?style=for-the-badge&logo=google&logoColor=white)
![Router](https://img.shields.io/badge/Multi--Provider%20Router-Groq%20%7C%20OpenRouter-orange?style=for-the-badge)
![License](https://img.shields.io/badge/License-CC%20BY--NC%204.0-green?style=for-the-badge)

<br>

### 👑 Original Developer: [FatihMakes](https://www.youtube.com/@FatihMakes)
### 🛠️ Enhanced & Maintained by: **MH SAMI**

[![YouTube](https://img.shields.io/badge/YouTube-@FatihMakes-red?style=for-the-badge&logo=youtube&logoColor=white)](https://www.youtube.com/@FatihMakes)
[![Instagram](https://img.shields.io/badge/Instagram-@fatihmakes-E4405F?style=for-the-badge&logo=instagram&logoColor=white)](https://www.instagram.com/fatihmakes)

</div>

---

> 💎 **Credits & Enhancements**
>
> Original architecture and foundation: **FatihMakes**.
> This build is enhanced and maintained by **MH SAMI**, who developed the
> **Universal Learning Station (Study Mode)** — the interactive Smart Board with
> step-by-step solving, 2D mindmaps, 3D flashcards, quizzes, zero-cost local
> caching, and structured workspace exports.

---

## 📑 Table of Contents

| # | Section |
|---|---|
| 1 | [✨ Core Features & Capabilities](#capabilities) |
| 2 | [🎓 Universal Learning Station (Study Mode)](#study-mode) |
| 3 | [🔑 Multi-Provider Router & Key Rotation](#router) |
| 4 | [🚀 Quick Installation & Setup](#install) |
| 5 | [⚙️ Configuration](#config) |
| 6 | [🗂️ Project Structure](#structure) |
| 7 | [📋 Requirements](#requirements) |
| 8 | [🔒 Your Data](#data) |
| 9 | [⚠️ License & Credits](#license) |

---

<a id="capabilities"></a>
## ✨ Core Features & Capabilities

| Capability | Description |
|---|---|
| 🎙️ **Real-Time Voice Streaming** | Bidirectional, low-latency audio over the **Gemini Live API** — natural interruptions (barge-in), echo suppression, noise gate, and turn tuning. |
| 🧠 **Multi-Provider Router** | Automatic fallback ladder across **Gemini, Groq, and OpenRouter** with per-key health tracking and cooldowns. |
| 🎓 **Universal Learning Station** | HSC/Bangla/English academic solver, quizzes, mindmaps, and flashcards on an embedded Smart Board. |
| 🖥️ **Computer Control** | Opens apps, drives the mouse/keyboard, changes system settings, and automates desktop workflows. |
| 💬 **Automated WhatsApp** | UIA-grounded WhatsApp Desktop controller that sends messages to chats and groups. |
| 👁️ **Screen Understanding** | Screen capture + OCR + vision to read papers, PDFs, and on-screen UI, then act on them. |
| 🌐 **Web & Browser Automation** | Playwright-driven browsing, search, and page interaction. |
| 🗂️ **Files & Documents** | Reads/edits files, PDFs, Office docs, and summaries; recoverable (recycle-bin) deletes. |
| 📊 **Remote Dashboard** | Phone control via LAN/HTTPS + Cloudflare tunnel with encrypted commands and QR/PIN pairing. |
| 🧩 **Plugin System** | Drop-in plugins loaded from `plugins/` with up-front dependency install. |
| 🎛️ **Cross-Platform HUD** | PyQt6 heads-up display with a reactive **Arc Reactor Core** and live waveform. |
| 🧠 **Memory** | Long-term memory, reminders, proactive suggestions, and a local study cache. |

---

<a id="study-mode"></a>
## 🎓 Universal Learning Station (Study Mode)

An interactive **Smart Board** (`runtime/study_board.html` in an embedded
`QWebEngineView`) driven by voice, with KaTeX LaTeX rendering.

| Feature | What it does |
|---|---|
| 🧮 **HSC Science Solver** | Solves NCTB **HSC Physics, Chemistry, Higher Math, Biology, ICT** (and custom subjects) as structured step-by-step solution cards. |
| ➗ **Interactive LaTeX Math** | Bangla/English derivation rendered live with LaTeX, step-by-step UI cards you can advance (`next` / `prev`). |
| 🌳 **2D Concept Mindmaps** | Branching visual concept trees with LaTeX equation support. |
| 🃏 **3D Flashcards** | High-yield flip cards for rapid revision. |
| ✅ **Anti-Cheat MCQ Quizzes** | Keyboard-first quiz engine with a Study Mode safety lock (`study_guard`) that disables the webcam while active; full explanations and mistake review after submission. |
| 📝 **Notepad & Export** | Evaluate notes (`Ctrl+N`), export sessions/solutions to Word (`.docx`) and a structured workspace. |
| 🗄️ **Zero-Cost Cache** | Local caching (`memory/study_cache.py`) reuses solved questions free of API cost. |

**Voice/tool control:** `study_station_control` handles `enter`, `exit`, `next`,
`prev` / `back`, `restore`, `clear`, `check_notes`, `export`, and `review_mistakes`.

---

<a id="router"></a>
## 🔑 Multi-Provider Router & Key Rotation

`core/model_router.py` runs every one-shot action and Study Mode call through an
ordered **fastest-first, non-thinking** ladder, and rotates across multiple API
keys per provider.

| Provider | Role in the ladder | Keys |
|---|---|---|
| 🟢 **Groq** | Fastest rung — `llama-3.1-8b-instant`, `llama-3.3-70b-versatile` | `groq_api_key` + `groq_api_keys[]` |
| 🔵 **Gemini** | Direct REST — `gemini-2.5-flash-lite`, `gemini-2.5-flash`, `gemini-3.1-flash-lite` | `gemini_api_key` + `gemini_api_keys[]` |
| 🟣 **OpenRouter** | Free-tier fallback — `meta-llama/llama-3.3-70b-instruct:free` | `openrouter_api_key` |

**Key health engine**
- Keys are **numbered** for humans: `Key #2 (gsk_…4f1a)` — "Key #2 is cooling" is actionable.
- A rate-limited / quota-exhausted pair is put on **cooldown** and rotation skips it, then returns automatically.
- If the whole direct chain is exhausted or unconfigured, it falls back to the **`core.gemini` Live ladder** — so Study Mode never fails while a Gemini key exists.

---

<a id="install"></a>
## 🚀 Quick Installation & Setup

```bash
# 1. Clone the repository
git clone https://github.com/MHSAMI/JARVIS-MARK-LX.git
cd JARVIS-MARK-LX

# 2. Create and activate a virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. Install dependencies + Playwright browsers (OS-specific extras auto-filtered)
python setup.py

# 4. Add your API keys, then launch
python main.py
```

> 💡 `python setup.py` installs only this OS's packages (pip `sys_platform`
> markers) and fetches the Playwright browsers. Requires **Python 3.11–3.13**
> (3.12 recommended). The optional "Hey Jarvis" wake word is opt-in from
> **⚙ → WAKE WORD** inside the app.

---

<a id="config"></a>
## ⚙️ Configuration

Edit **`config/api_keys.json`**:

```json
{
  "gemini_api_key": "…",
  "gemini_api_keys": ["…", "…"],
  "groq_api_key": "…",
  "groq_api_keys": ["…", "…"],
  "openrouter_api_key": "sk-or-…",
  "assistant_name": "JARVIS",
  "user_name": "MH",
  "voice_name": "Charon",
  "os_system": "windows",
  "ui_color": "#00d4ff"
}
```

| Field | Purpose |
|---|---|
| `gemini_api_key` / `gemini_api_keys` | Live API + primary router keys (first key + extra rotation pool). |
| `groq_api_key` / `groq_api_keys` | Fast Groq rungs + key rotation pool. |
| `openrouter_api_key` | Free-tier fallback provider. |
| `os_system` | `windows` \| `mac` \| `linux` (auto-detected if absent). |
| `input_device` / `output_device` | Optional audio device overrides. |
| `ui_color` | HUD accent color. |

Keys can also be managed in-app from the settings screen, which writes both the
single key and the numbered pool.

---

<a id="structure"></a>
## 🗂️ Project Structure

| Path | Purpose |
|---|---|
| `main.py` | Entry point — Live session loop, orchestration, HUD startup. |
| `ui.py` | PyQt6 HUD, settings screens, **Arc Reactor Core** and icon rendering. |
| `setup.py` | One-time, OS-aware dependency + Playwright installer. |
| `core/` | Engine: `gemini.py`, `model_router.py`, `llm_client.py`, `study_canvas.py`, `study_guard.py`, `screen_agent.py`, `ui_grounding.py`, `stt.py`, `tts.py`, `noise_gate.py`, `wake_word.py`, and more. |
| `actions/` | Callable tools — `study_solver.py`, `study_quiz.py`, `study_mindmap.py`, `study_flashcard.py`, `study_controller.py`, `send_message.py`, `computer_control.py`, `screen_processor.py`, `browser_control.py`, `file_processor.py`, `dev_agent.py`, `weather_report.py`, and more. |
| `plugins/` | Auto-loaded extensions: `study_mentor.py`, `_whatsapp_core.py`, `file_access.py`, `file_navigator.py`, `temp_cleaner.py`. |
| `memory/` | `memory_manager.py` (long-term), `study_cache.py`, `config_manager.py`, `long_term.json`. |
| `config/` | `api_keys.json`, `credentials.json`, `jarvis.ico`, certs, and the runtime log. |
| `dashboard/` | Local/global web dashboard server, login, and static UI. |
| `runtime/` | `study_board.html` Smart Board plus vendored KaTeX, Tailwind, and Plotly. |
| `tools/` | Dev utilities — `calibrate.py`, `target_debug.py`, `fetch_study_vendor.py`. |
| `bin/` | Bundled binaries (e.g. `cloudflared.exe`). |
| `requirements.txt` | Cross-platform dependency list with OS markers. |
| `workspace/` | Exports (session notes, `.docx` solutions). |

---

<a id="requirements"></a>
## 📋 Requirements

- **Python 3.11–3.13** (3.12 recommended)
- **Windows 10/11**, **macOS**, or **Linux**
- Microphone + speakers (audio streaming)
- A **Gemini API key** (Live API); Groq / OpenRouter keys optional but recommended
- `python setup.py` installs everything else per-OS

---

<a id="data"></a>
## 🔒 Your Data

- API keys live only in `config/api_keys.json` — never sent anywhere but the providers you configured.
- Memory, study cache, and exports stay local under `memory/` and `workspace/`.
- Webcam is locked while Study Mode is active (`core/study_guard.py`).

---

<a id="license"></a>
## ⚠️ License & Credits

**License:** [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) — commercial use is **not** permitted. See [`LICENSE`](LICENSE).

```text
JARVIS — MARK LX
Copyright (c) 2026 FatihMakes — original architecture & foundation
```

| Role | Person |
|---|---|
| 👑 Original Developer | **FatihMakes** |
| 🛠️ Maintainer & Study Mode | **MH SAMI** |

<div align="center">

**⭐ If MARK LX helps you, star the repo.**

</div>
