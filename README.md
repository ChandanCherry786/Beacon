<p align="center">
  <img src="Logo_icon.png" alt="Beacon" width="128">
</p>

<h1 align="center">Beacon</h1>

<p align="center">A local research IDE for LaTeX manuscripts and Python code, built for researchers.</p>

Beacon gives you a file tree over any folder, a tabbed editor with Overleaf-style autocomplete and syntax highlighting, embedded PDF and image viewing, a live Markdown reader, one-click LaTeX compile with live preview and automatic package installation, a real interactive terminal, per-folder git with one-click multi-remote sync, an AI assistant with a choice of model (local Ollama, Claude, ChatGPT, Gemini, or Grok), a research finder over OpenAlex and Crossref with publisher-verified authors, Word and PDF export from LaTeX or Markdown, Python formatting and notebook execution, and a Pomodoro focus timer.

It is a single Python file plus one page, with one optional dependency (pywinpty, for the interactive terminal). The interface runs in a Microsoft Edge application window (or your default browser), served locally from `127.0.0.1:8347`. Nothing leaves your machine except the lookups and installs you explicitly trigger; any AI API keys you add stay in a git-ignored local file and are never committed.

## Features

- **Open any folder.** No fixed project structure is required. Recent workspaces, quick shortcuts to Home, Documents, Downloads, OneDrive, and Dropbox, and a native folder picker.
- **Tabbed editor** with syntax highlighting for LaTeX (commands, section headers, `\cite`/`\ref`, environments, math), Python, BibTeX, Markdown, and JSON. Line numbers, word count, and cursor position. Overleaf-style autocomplete for LaTeX commands and environments and for Python keywords, auto-closing brackets and `$`, automatic `\end` when you open a `\begin{}`, block indent with Tab, and comment toggling with Ctrl+/.
- **Citation autocomplete.** Typing inside `\cite{}` (and `\citep`, `\citet`, `\parencite`, and the rest) suggests the citation keys defined in the workspace `.bib` files, each labelled with its author, year, and title, and inserts the key on selection. The key list refreshes when a `.bib` is saved or tidied.
- **Cross-reference autocomplete.** Typing inside `\ref{}`, `\eqref{}`, `\cref{}`, and the other reference commands suggests the `\label` names defined in the document, merging labels in the current buffer with those known from other `.tex` files so a reference completes without hunting for the exact label.
- **Document outline.** A sidebar Outline tab lists the sectioning of the active `.tex` (`\part` through `\subparagraph`) or the classes and functions of the active `.py`, indented by level, and jumps the editor to any entry on click. For a main file it follows `\input` and `\include` into the section files, so the whole document appears and a click opens the right section file at the right line. It follows the active tab and updates as you edit.
- **Embedded PDF and image viewing.** Open any PDF or image (`.png`, `.jpg`, `.svg`, and more) as a tab and view it beside your source. PDF reading position is preserved across tab switches.
- **Markdown reader.** Open any `.md` file and its rendered view (headings, tables, code, blockquotes, links) appears live in the Preview pane beside the source, updating as you type and matching the theme.
- **Word and PDF export.** The Tools menu (or Ctrl+Shift+E) converts your LaTeX paper to a `.docx` with Pandoc, preserving structure, headings, tables, equations, and citations. Before conversion it renders vector (PDF/EPS) figures to 300-DPI PNG with PyMuPDF so plots and diagrams appear in Word, applies a Times New Roman template with heading styles and one-inch margins for a consistent look, and sets two columns when the source is two-column. Word cannot reproduce a LaTeX layout exactly, so the result is close rather than identical. A Markdown file exports the same way to Word or to PDF, with a workspace `.bib` wired in through citeproc.
- **LaTeX compile with live preview.** Compile the active `.tex` and the typeset page appears in the Preview pane. Compiling a section fragment builds the main document automatically. Missing packages are detected and installed with `tlmgr` without interrupting the build.
- **Compile problem list.** After a build, the `.log` is parsed into a clickable list of errors and warnings, each with its file and line, tracking the log's file stack so a problem inside an `\input` section points to that section rather than the main file. A failed build opens the list; the Tools menu reopens it any time.
- **Insert citations while writing.** In the Research Finder, one action adds a result to `references.bib` and drops the matching `\cite{key}` at the editor cursor, so finding and citing a paper is a single step.
- **LaTeX snippets.** The Tools menu inserts ready-made `figure`, `table`, `equation`, `align`, list, `algorithm`, and two-panel `subfigure` scaffolds at the cursor, each with `\caption` and `\label` stubs in place.
- **Writing progress.** The Tools menu tracks the total word count of the workspace and how many words are added each day against a configurable daily goal, shown with a progress bar and a short recent-day history. The goal is set in Settings.
- **Run Python** with your configured interpreter, output streamed live, with a Stop button.
- **Run Jupyter notebooks.** Right-click a `.ipynb` in the file tree to execute every cell with nbconvert and save the outputs back into the file, with errors streamed to the Output panel.
- **Format Python.** The Tools menu or the file tree reformats the file with Ruff, or Black when Ruff is absent, and reloads the editor from disk.
- **Tidy BibTeX.** Right-click a `.bib` to drop entries with duplicate citation keys, sort the rest by key, and align every field, flagging entries that share a DOI. The original is copied to a `.bak` first, and a file whose braces do not balance is left untouched.
- **Citation and reference check.** The Tools menu cross-checks the `\cite` keys used in a paper (following `\input` and `\include`) against the entries defined in the workspace `.bib` files, and the `\ref` keys against the `\label` definitions, then reports citations or references with no matching target and entries or labels that are never used, so missing and dead references surface before submission.
- **Prose check.** The Tools menu scans a `.tex` for common academic-writing weaknesses (em-dashes, intensifiers, hedging and filler phrases, trailing gerund clauses, sentence-initial "However,", duplicated words, and likely passive voice) and lists each flagged line in the Search panel, click to jump. The checks are advisory and ignore comments and command markup.
- **TODO and note tracker.** The Tools menu collects every `TODO`, `FIXME`, `XXX`, `HACK`, `BUG`, and `\todo` marker across the workspace source files into a clickable list in the Search panel, so loose ends are one click away.
- **LaTeX word count and build cleanup.** The Tools menu estimates the word count of a paper, following `\input` and `\include` while excluding commands, math, and comments, and removes regenerable build artifacts (`.aux`, `.log`, `.bbl`, `.synctex.gz`, and the rest) without touching source files.
- **Full interactive terminal** in the workspace, rendered with xterm.js over a real pseudo-console (ConPTY via pywinpty). Interactive tools such as `claude`, `vim`, tab-completion, and colored output work inline. Without pywinpty it falls back to a simpler line-based terminal, so it always works.
- **Git per folder.** The status badge follows the active file to its nearest repository, classifies each remote as GitHub, Overleaf, or local, and offers commit, pull, push, and fetch. One-click **Publish** (the Sync button, or Ctrl+Shift+S) commits, pulls, then pushes. When a folder has both a GitHub remote and an Overleaf remote it asks where to send the work in plain terms, Overleaf (documents) or GitHub (code) or both, so writing and code publish on your choice; with a single remote it just publishes, and with none it offers to connect one.
- **Connect a folder to Overleaf or GitHub** (git menu, or Ctrl+Shift+O). Paste the Overleaf git URL and sync token to push a paper folder straight to your Overleaf project, or paste a GitHub repo URL and personal access token to push a code folder to GitHub. A document folder can go to Overleaf and a computational folder to GitHub, each with its own remote, so writing and code publish independently. The dialog links to where each URL and token live. The token is stored only inside that folder's local `.git/config` remote URL, never in the app config, never sent back to the page, and stripped from any error message; the connection is verified against the server before it is saved.
- **Update from inside the app.** Because Beacon is installed by cloning its GitHub repository, Settings → Software updates checks that repository for new versions and, when the local copy is a clean checkout, fast-forwards to the latest with one click. The update never overwrites local edits (it refuses rather than force) and never touches the git-ignored config, so paths and keys survive. A **Restart Beacon** button then applies it: the server pulls the latest, relaunches, and the window reloads in place onto the fresh instance, so there is no need to hunt for the process or double-launch the batch file.
- **AI assistant** with a choice of model. Local open-source models through [Ollama](https://ollama.com) are free and need no key (they appear automatically when Ollama is running). Claude models (Fable, Opus, Sonnet, Haiku) run through the Claude Code CLI and can edit files in the workspace. ChatGPT, Gemini, and Grok work as chat once you add your own API key in Settings. API keys are stored only in the git-ignored local config and are never committed. The assistant receives the open file inline so it acts precisely, and can be stopped mid-task.
- **Research Finder, tuned for power and electrical engineering.** Search papers, books, and preprints by keyword, author, title, or DOI across **OpenAlex, Semantic Scholar, and Crossref**, either one at a time or all together in a merged view that de-duplicates the same work by DOI or title and combines its abstract, citation count, and open-access link from whichever source has them. Results are **ranked by relevance to the query first**: pasting a paper's title surfaces that paper at the top, and a query that blends title words with an author name matches on both, so the closest paper wins rather than the most-cited one. Pasting a **DOI** resolves that exact record directly. Peer-reviewed work is preferred over preprints and citations act only as a secondary tiebreaker, with an "EE venues first" option that lifts major power-systems and electrical-engineering venues (IEEE Transactions, Elsevier energy journals, IET, and similar) and a "Power & EE only" filter that restricts to them. Every result card shows its source, publisher, citation count, open-access status, and a preprint marker where it applies; only fields the sources actually return are shown. Author names are corrected against the authoritative publisher record. Copy BibTeX (properly formatted) or a reference in IEEE, Vancouver, APA, MLA, Chicago, Harvard, or Nature style, append to `references.bib` with de-duplication, insert a `\cite` at the cursor, or open the paper online through a real link.
- **Pomodoro focus timer** in the toolbar with configurable focus and break lengths and a session counter.
- **Dark and light themes**, five accent colors, and a choice of editor fonts.
- **Quick open** (Ctrl+P), **search in files** (Ctrl+Shift+F), file create, rename, and delete, resizable panes, and session tab restore.

## Requirements

The application code needs only Python and its standard library. The features below depend on external tools that are expected to be on `PATH`. Each tool is optional and affects only its own feature.

| Tool | Purpose | Required for |
| --- | --- | --- |
| Python 3.9+ (with tkinter) | Runs the server and your Python files | Everything |
| pywinpty (pip, optional) | Real interactive terminal (falls back to line terminal without it) | Interactive terminal |
| Ruff or Black (pip, optional) | Reformat Python files from the Tools menu | Python formatting |
| Jupyter with nbconvert (pip, optional) | Execute `.ipynb` notebooks and save outputs in place | Notebook run |
| Git | Status, commit, pull, push, sync | Git features |
| A TeX distribution with `latexmk` and `tlmgr` (TinyTeX or TeX Live) | LaTeX compilation, package install, and the PDF engine for Markdown export | Compile, Markdown to PDF |
| Claude Code CLI (`claude`) | The Claude models in the assistant panel | Claude assistant |
| Pandoc (optional) | Word (`.docx`) and PDF export from LaTeX or Markdown | Word and PDF export |
| Ollama (optional) | Free local open-source AI models, no key | Local AI |
| Microsoft Edge | The application window (falls back to your default browser) | Preferred |
| Internet access | Crossref/OpenAlex/doi.org lookups, `tlmgr` and hosted-AI calls | Finder, auto-install, hosted AI |

The toolkit was developed and verified on Windows 11 with Python 3.13.7, git 2.54, latexmk 4.88 on TinyTeX, and Claude Code 2.1.206. The terminal is PowerShell, so the launcher and terminal features assume Windows.

## Getting started

```bash
git clone https://github.com/ChandanCherry786/Beacon.git
cd Beacon
```

**First-time setup (recommended).** Double-click `setup.bat` (or run `powershell -ExecutionPolicy Bypass -File setup.ps1`). It checks for Python, offers to create a local virtual environment, and offers to install anything missing, including a LaTeX distribution (TinyTeX). It never installs anything without asking, and it is safe to run more than once.

Then launch the app.

**Double-click** `run_beacon.bat`. It starts the server without a console window and opens the application window. It uses the `.venv` from setup if present, otherwise any Python on your PATH.

**Or from a terminal:**

```powershell
python beacon.py
```

Add `--no-browser` to start the server without opening a window.

On first launch, choose a workspace folder. The Run button and terminal use the Python interpreter that launched the server by default; change it any time in Settings.

**Updating.** Open Settings → Software updates and click Check for updates, then Update now, and restart. This is equivalent to running `git pull` in the install folder, so a copy obtained by cloning stays current with the project.

## Usage

Click any file in the tree to open it, or a PDF to read it. Compile the active `.tex` with the Compile button or Ctrl+B, and the result appears in the Preview pane. Run the active `.py` file with the Run button. The bottom dock holds the terminal and the compile, run, and git output, each with a Stop button. The right pane switches between the compiled Preview and the Claude assistant.

### Keyboard shortcuts

| Shortcut | Action |
| --- | --- |
| Ctrl+S | Save the active file |
| Ctrl+B | Compile the active `.tex` |
| Ctrl+/ | Toggle line comment (LaTeX `%`, Python `#`) |
| Ctrl+Space | Force autocomplete suggestions |
| Tab / Shift+Tab | Indent / outdent (or accept a suggestion) |
| Ctrl+P | Quick open a file by name |
| Ctrl+Shift+F | Search in files |
| Ctrl+Shift+K | Research Finder |
| Ctrl+Shift+E | Export LaTeX to Word |
| Ctrl+Shift+S | Publish (commit, pull, then push; asks Overleaf or GitHub when both exist) |
| Ctrl+Shift+O | Connect the folder to Overleaf or GitHub |
| Ctrl+` | Toggle the bottom dock |
| Ctrl+, | Open Settings |
| Ctrl+Enter | Send the Claude message |
| Esc | Close the open dialog or menu |

### Settings

Open Settings with the gear icon or Ctrl+,. You can set the theme, accent color, editor font and size, the LaTeX compiler (latexmk, a single pdfLaTeX pass, XeLaTeX, or LuaLaTeX), automatic installation of missing LaTeX packages, the Python interpreter, and auto-save. Settings persist across sessions.

## Working with AI coding agents in the terminal

Beacon's terminal is a real interactive shell that opens in your workspace folder, so you can run agentic command-line AI tools directly inside it and let them help you write and revise your work.

Type the tool's command in the terminal and it launches with full access to the files you are editing:

- **Claude Code** — `claude`
- **OpenAI Codex CLI** — `codex`
- **Gemini CLI**, **Aider**, or any other terminal AI agent — their usual command

Because the session runs in your workspace, the agent can read and edit your actual `.tex`, `.bib`, and `.py` files: draft a section, tighten prose, fix and format citations, draft responses to reviewers, refactor analysis code, or run experiments. Beacon's editor and preview update live as the agent changes files on disk, so you watch the work happen and keep editing alongside it. Save your files first (Ctrl+S) so the agent sees your latest text, and commit or sync through the git menu when you are happy with the result.

The built-in **Get help with AI** panel is the lighter alternative for single questions with a model picker (local Ollama, Claude, ChatGPT, Gemini, or Grok); the terminal is for full agent sessions.

The interactive terminal needs the optional `pywinpty` package (installed by setup). Without it, run these agents from the **Real terminal** button in the terminal dock, which opens an external console in the same folder.

## Security model

Beacon runs a local server, so it is built to resist a hostile web page you might have open in the same browser.

- **Localhost only.** The server binds to `127.0.0.1`. Every request's `Host` header must be `127.0.0.1`/`localhost`, which defeats DNS rebinding: a malicious site that repoints its hostname at your machine still sends its own domain and is refused.
- **Per-launch token.** A secret generated at startup is injected into the page and required on every data request (and on the terminal WebSocket). A page on another origin cannot read the page, so it cannot learn the token.
- **Origin checks.** Every state-changing request and the terminal WebSocket also require a same-origin `Origin`.
- **Workspace confinement.** File, PDF, and image access is restricted to the current workspace folder (validated with resolved-path containment, so `..`, symlinks, and UNC tricks are rejected).
- **API keys stay local.** Keys for ChatGPT, Gemini, and Grok live only in the git-ignored config, are never committed, are redacted from every server response (only a "set/not set" flag is returned), and are stripped from any error message. Gemini's key travels in a request header, not a URL. There is no Claude key: the Claude models use your own authenticated Claude Code CLI, capped per task by `--max-budget-usd`.
- **Constrained AI edits.** The Claude panel runs with `--permission-mode manual` and a restricted tool allowlist, so it can edit files in the workspace and run `latexmk`, `python`, `pdflatex`, and `bibtex`, while out-of-scope write commands are denied.
- **Safe rendering.** Markdown previews render in a sandboxed frame with scripts disabled, so an untrusted `.md` cannot execute anything or read the token.

## Project layout

```
beacon.py                 Local HTTP server and all backend logic
workbench.html            Single-page front end (HTML, CSS, vanilla JS)
sw.js                     Service worker (makes the app installable)
vendor/                   Bundled xterm.js terminal and marked.js markdown renderer
run_beacon.bat            Windows launcher
setup.bat / setup.ps1     First-time setup (checks tools, installs deps)
Logo_icon.png, *.png, favicon.ico, manifest.webmanifest   App icons and PWA manifest
workbench_config.json     Saved state and API keys (created on first run, git-ignored)
README.md                 This file
requirements.txt          Dependencies (one optional pip package)
LICENSE                   MIT
```

## Troubleshooting

**A push to Overleaf or a private remote fails with an authentication error.** The remote has no stored credentials. Use the git menu's **Connect Overleaf or GitHub** (Ctrl+Shift+O) to attach the folder with a token, or run one manual `git push` in that repository first so the credential manager caches the token. The app never blocks on a credential prompt: git is run with prompts disabled, so a missing or wrong credential returns a clear error instead of hanging.

**A package fails to install automatically.** The log shows the `tlmgr` command that failed. Run it yourself in the terminal, for example `tlmgr install <package>`, then recompile.

**The Claude panel returns an error immediately.** Confirm `claude` is on `PATH` and signed in by running `claude --version` in a terminal.

**The window does not open on launch.** Start the server manually and open `http://127.0.0.1:8347`. If the port is already in use, an instance is likely already running.

## Limitations

The interactive terminal needs the optional `pywinpty` package (installed by setup). Without it, the terminal falls back to a line-based mode that runs commands and streams output but cannot host interactive full-screen programs; in that mode, use the **Real terminal** button to open an external console, or the AI panel for Claude. The Claude assistant can be restricted from out-of-scope writes, but Claude Code always permits read-only commands such as `git status`, which is a property of the CLI rather than this app. The Claude assistant can be prevented from making out-of-scope writes, but Claude Code always permits read-only commands such as `git status`, which is a property of the CLI rather than this app.

## Version and license

Beacon v1.0.0. Copyright (c) 2026 Chandan Chaudhary. Released under the MIT License. See `LICENSE`.

Beacon runs from `http://127.0.0.1`, so when it is installed as a desktop app the browser labels the source by that local address rather than a signed publisher name; a locally served application has no code-signed publisher. The author, version, and copyright above are the software's identity and appear in Settings.
