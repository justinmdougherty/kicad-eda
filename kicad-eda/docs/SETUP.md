# Setup Guide — from nothing to your first board

This guide assumes you have never used Claude Code, never cloned a GitHub
repo, and just want to chat with Claude and get a circuit board out. Follow
it top to bottom. Windows instructions are primary; macOS notes are inline.

When you're done you will type a sentence like *"design me a USB-C powered
LED driver"* into a terminal and get back a finished KiCad project and a
zip file you can upload to a board fab.

---

## What you're installing, in plain terms

| Thing | Why you need it |
|---|---|
| **Claude Code** | The AI chat app this plugin runs inside. Lives in your terminal. |
| **Git** | Lets your computer download ("clone") the private repo you've been invited to. |
| **KiCad 10** | The PCB design suite. The plugin drives it headlessly — you only open it at the end to look at your board. |
| **KiCad 11 nightly** *(optional)* | Lets KiCad itself draw the schematic through its new API instead of the plugin writing the file by hand. Everything works without it. |
| **Java** | Runs the autorouter (the program that draws the copper traces). |
| **Python 3** | Runs the plugin's design tools. |
| **The kicad-eda plugin** | The thing that ties it all together. |

Total time: roughly 30–45 minutes, most of it watching installers.

---

## Step 1 — Accept the GitHub invitation

The repo is **private**, so you must be invited before anything works.

1. You'll get an email from GitHub: *"justinmdougherty invited you to
   collaborate on justinmdougherty/kicad-eda"*. Click **View invitation**,
   then **Accept invitation**.
   - No email? Log in at <https://github.com> and check
     <https://github.com/notifications> — the invite appears there too.
2. Confirm it worked: visit
   <https://github.com/justinmdougherty/kicad-eda> while logged in. If you
   see the code instead of a 404 page, you're in.

---

## Step 2 — Install Git

Git is what downloads the repo and proves to GitHub that you're you.

**Windows:**
1. Open the **Start menu**, type `powershell`, press Enter. A blue/black
   text window opens — this is your terminal. You'll use it a lot today.
2. Paste this and press Enter:
   ```
   winget install --id Git.Git -e
   ```
3. When it finishes, **close PowerShell and open a new one** (installers
   change settings that only fresh windows pick up — you'll do this several
   times today).
4. Check it worked:
   ```
   git --version
   ```
   You should see something like `git version 2.45.0`. If you see *"git is
   not recognized"*, close the window and open a new one.

**macOS:** open Terminal (Cmd+Space, type `terminal`) and run
`xcode-select --install`, or `brew install git` if you have Homebrew.

---

## Step 3 — Install Claude Code

1. Go to <https://claude.com/claude-code> and follow the install
   instructions for your platform, or in PowerShell:
   ```
   winget install --id Anthropic.ClaudeCode -e
   ```
   (macOS: `brew install --cask claude-code`)
2. Open a **new** terminal window and start it:
   ```
   claude
   ```
3. The first run walks you through logging in with your Claude account in a
   browser. You need a paid Claude plan (Pro or Max) or an API key.
4. When you see the Claude Code prompt (a `>` waiting for input), type
   `/exit` and press Enter — we'll come back after installing the rest.

---

## Step 4 — Install KiCad 10

> Already have KiCad? **Check the version**: open KiCad → Help → About
> KiCad. If it says **9.x or older, upgrade** — the pipeline is validated
> against KiCad 10 and parts of it will fail on 9. Installing 10 alongside
> an older version is fine.

**Windows:**
```
winget install --id KiCad.KiCad -e
```
This is a multi-gigabyte download — let it run. Default options are fine.

**macOS:** `brew install --cask kicad` or download from
<https://www.kicad.org/download/>.

When it finishes, open KiCad once (Start menu → KiCad 10) and let it do its
first-run setup. Accept the defaults. You can close it afterward.

### Optional: KiCad 11 nightly (schematic API)

With a KiCad 11 nightly installed **alongside** KiCad 10, the plugin builds
schematics through KiCad's own API (KiCad places the symbols and writes the
file) and can even edit a schematic you have open in the KiCad 11 editor.
Skip this if you just want boards — the plugin falls back to writing the
schematic file itself.

1. Download the Windows nightly from
   <https://downloads.kicad.org/kicad/windows/explore/nightlies> (macOS and
   Linux nightlies are one level up on the same site) and install it. It
   lands in `C:\Program Files\KiCad\10.99` (a KiCad 11 nightly calls itself
   version 10.99) and does not touch your KiCad 10.
2. **Open KiCad 11 once** and let it do its first-run setup, so it has its
   library tables. Nothing breaks if you skip this: a nightly without them
   cannot place stock symbols, so the pipeline automatically writes the
   schematic file itself instead, as without the nightly (a KiCad 10 file;
   its result's `fallback` entry says so, and the choice is logged in the
   project's `<stem>.decisions.jsonl`). The plugin never edits KiCad's
   settings for you. (Alternative to opening it: copy `sym-lib-table` and
   `fp-lib-table` from `C:\Program Files\KiCad\10.99\share\kicad\template`
   into `%APPDATA%\kicad\10.99`.)
3. Keep KiCad 10: the board build still needs its bundled Python, which the
   nightly no longer ships. It also limits which footprint files a board can
   use: KiCad 9 loads footprints up to `(version 20241229)` and KiCad 10 up to
   `(version 20260206)`; one saved by KiCad 11 is newer still. Parts the
   plugin generates for you are written in KiCad 9's format, so they always
   load (see `docs/LIBRARIES.md` in the repo).

Schematics made this way are KiCad 11 files — open them in KiCad 11, not 10.

---

## Step 5 — Install Java

The autorouter is a Java program (the plugin bundles the router itself —
you just need Java to run it).

**Windows:**
```
winget install --id EclipseAdoptium.Temurin.21.JRE -e
```

**macOS:** `brew install --cask temurin@21`

Check (in a **new** terminal window):
```
java -version
```
You want to see `openjdk version "21..."` (17 through 21 all work).

---

## Step 6 — Install Python

**Windows:**
```
winget install --id Python.Python.3.12 -e
```

> ⚠️ **Windows gotcha:** if you skip this because "Python is already
> there", make sure typing `python --version` in a new terminal prints a
> real version number. Windows ships a fake `python` that opens the
> Microsoft Store — that one doesn't count.

**macOS:** `brew install python`

Check (new terminal):
```
python --version
```
Expect `Python 3.12.x` (3.10 or newer is fine).

You do **not** need to install any Python packages — the plugin creates its
own private Python environment automatically the first time it starts.

---

## Step 7 — Clone the repo (and sign in to GitHub once)

This step downloads the code *and* teaches your computer your GitHub
credentials, which the plugin install in Step 8 needs.

1. Open a new PowerShell window.
2. Go somewhere sensible, e.g. your home folder:
   ```
   cd ~
   ```
3. Clone:
   ```
   git clone https://github.com/justinmdougherty/kicad-eda
   ```
4. A window pops up asking you to sign in to GitHub — click **Sign in with
   your browser** and approve it. This happens only once; Windows remembers
   it afterward. (macOS: you may be asked for a username and a "personal
   access token" instead — create one at GitHub → Settings → Developer
   settings → Personal access tokens, scope `repo`.)
5. Success looks like a `kicad-eda` folder appearing:
   ```
   cd kicad-eda
   dir
   ```
   You should see `README.md`, a `kicad-eda` folder, a `Dockerfile`, etc.

---

## Step 8 — Install the plugin

1. Start Claude Code from anywhere:
   ```
   claude
   ```
2. At the Claude Code prompt, type these two commands (they start with `/`
   — type them into the chat exactly as shown, one at a time):
   ```
   /plugin marketplace add justinmdougherty/kicad-eda
   ```
   then:
   ```
   /plugin install kicad-eda@dougherty-eda
   ```
3. **Exit and restart Claude Code** (`/exit`, then `claude` again).
   Plugins load at startup, so this matters.

If `marketplace add` complains it can't reach the repo, Step 7's sign-in
didn't stick — run the `git clone` again and watch for the login popup. As
a fallback you can point it at your local clone instead:
`/plugin marketplace add C:\Users\YOURNAME\kicad-eda` (use the folder from
Step 7).

---

## Step 9 — Let the plugin check everything

In Claude Code, run:

```
/kicad-eda:setup
```

Claude runs a checker that looks for KiCad, Java, and Python and reports
what it found. If something is missing it will offer to install it — say
yes, or use the matching step above. The happy ending looks like:

```
[ok] KiCad: 10.0.x
[--] KiCad 11 nightly not found (optional: schematic API backend) ...
[ok] Java: openjdk 21...
[ok] Python: 3.12.x
[ok] freerouting-1.9.0.jar present
```

The `[--]` line is informational — the nightly is optional (Step 4) and the
checker never installs it. With one installed it reads
`[ok] KiCad 11 nightly (optional, schematic API): 10.99.0-...`.

The first time the plugin's tools run, it also builds its private Python
environment — that one-time step takes a minute or two.

---

## Step 10 — Design your first board

Make a folder for your project and start Claude Code **in it** (outputs
land in the folder you started from):

```
mkdir ~/my-first-board
cd ~/my-first-board
claude
```

Then just describe what you want, either in plain chat or with the
command:

```
/kicad-eda:pipeline a USB-C powered board with an ESP32-C3, one button,
one status LED, and a temperature sensor on I2C
```

**What to expect:**

- Claude asks a few engineering questions first (how much current? does it
  need a battery? what connector?). Answer in plain English — "no battery,
  just USB power" is a fine answer.
- It then works through five review gates: requirements → schematic +
  electrical check → component placement → autorouting + design-rule check
  → fabrication package. You'll see each gate pass (or fix itself and
  retry).
- **The autorouting step can take several minutes** — a Java process is
  drawing every copper trace. Silence does not mean it's stuck.
- Claude Code will occasionally ask permission to run a tool. That's
  normal — approve it.

**What you get back**, in your project folder:

| File | What it is |
|---|---|
| `netlist.json` | The design's source of truth (Claude wrote it from your conversation) |
| `*.kicad_sch` | The schematic — open in KiCad, ERC is clean |
| `*.kicad_pcb` | The board — open in KiCad, routed, DRC is clean |
| `*_top.png` / `*_bottom.png` | Renders to eyeball without opening KiCad |
| `*_report.md` | Design-intent review (power tree, decoupling, etc.) |
| `*_JLCPCB.zip` | Gerbers + drill + BOM + placement — upload this to the fab |

Look at the renders first, then open the `.kicad_pcb` in KiCad and poke
around. If you want changes — "make the board smaller", "swap the sensor
for a BME280", "add mounting holes" — just say so; Claude edits the design
and re-runs only what changed.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `git`/`java`/`python` "not recognized" right after installing | Close the terminal and open a new one. Installers update the PATH but open windows don't see it. |
| `/plugin marketplace add` can't access the repo | You haven't accepted the GitHub invite (Step 1) or git isn't signed in (Step 7). Re-run the clone and watch for the login popup. |
| The `kicad-generator` MCP server shows as **failed** when Claude Code starts | Usually Python wasn't installed yet when the plugin first launched. Make sure `python --version` works, then restart Claude Code. |
| `kicad-cli not found` even though KiCad is installed | Tell the plugin where KiCad lives: in Claude Code run `/plugin` → kicad-eda → settings, and set **KiCad install path** (e.g. `C:\Program Files\KiCad\10.0` or `C:\Users\you\AppData\Local\Programs\KiCad\10.0`). |
| `java not found (Freerouting needs a JRE/JDK)` during routing | Step 5, then restart Claude Code. `java -version` must work in a fresh terminal. |
| Routing seems frozen for minutes | Normal for dense boards. The pipeline has its own timeout and will tell you if the router actually failed. |
| You have KiCad 9 | Upgrade to 10. The headless tools call KiCad APIs that changed between 9 and 10. |
| `symbol Device:R not found` (or "no libraries") right after installing the KiCad 11 nightly | The nightly has never been opened, so it has no library tables. The pipeline needs no action: it builds the schematic with its KiCad 10 file writer instead and says so (`fallback` in the result, logged in `<stem>.decisions.jsonl`). To use KiCad 11's schematic API — e.g. for the interactive schematic tools — open KiCad 11 once, or copy `sym-lib-table` and `fp-lib-table` from `C:\Program Files\KiCad\10.99\share\kicad\template` into `%APPDATA%\kicad\10.99` (Step 4). |
| A board build fails with `pcbnew ... could not load footprint ...`, `newer than (version N)` | The footprint file was saved by a newer KiCad than the one building the board: KiCad 9 loads footprints up to `(version 20241229)`, KiCad 10 up to `(version 20260206)`. Ask Claude to convert the library to KiCad 9's format or to generate the part itself (`docs/LIBRARIES.md`, section 7). |
| KiCad 10 refuses to open the `.kicad_sch` | It was written by KiCad 11 (the schematic API backend). Open it in KiCad 11, or ask Claude to rebuild it with `backend="file"` for a KiCad 10 file. |
| Something else | Copy the error into the chat — Claude can usually diagnose its own pipeline. The internals run-book is `docs/PIPELINE.md` in the repo. |

---

## What NOT to worry about

- **The `examples/` folder and the regression suite** (`pipeline/run_all.py`,
  the Dockerfile, the GitHub Actions workflow) are development/test
  infrastructure used to validate the process. You don't need them to
  design boards.
- **Editing `netlist.json` by hand.** You *can* (the schema is in
  `docs/PIPELINE.md`), but the whole point is that the conversation writes
  it for you.
- **The KiCad GUI being closed.** The pipeline never needs KiCad running —
  it uses KiCad's command-line tools. You only open KiCad to admire the
  result. (The one bonus of KiCad 11: if you have the schematic open in its
  editor with the API server enabled, Claude's schematic edits land right in
  that window.)
