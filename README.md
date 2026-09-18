# tex-manager

`texman` is a personal file manager for LaTeX files, with AI help for diagrams
and other hard-to-TeX stuff.

It scans `~/Documents` and `~/Downloads` for `.tex` and `.sty` files and shows
them in one searchable terminal catalog, grouped by directory. `--root` points
it somewhere else, or at several places at once. You can describe any file in
your own words, open it in Neovim, and — while editing — ask for a generated
LaTeX fragment with `:TexAI <line> <prompt>`.

Your files are never moved or copied, so relative `\input`, `\include`, image,
and style references keep working. The catalog and your descriptions stay on
your machine. Nothing is uploaded except the prompts you explicitly ask for.

See [ARCHITECTURE.md](ARCHITECTURE.md) for how it works internally.

## What it does

- One catalog of every `.tex` and `.sty` file under `~/Documents` and
  `~/Downloads`, including hidden folders and nested project directories.
- A description per file that survives restarts, rescans, stopped scans, and
  files that go missing.
- Directories you can ignore with one key, hiding a whole project tree from the
  catalog and from future scans, and restore just as easily.
- Enter opens the selected file in Neovim, in its own directory, and returns you
  to the same place in the catalog.
- Vim-style movement: `5j`, `3k`, `gg`, `G`, and `;s` to jump to the next
  directory.
- A `preamble.tex` template of your own, and `n` to start a new document from
  it in any directory.
- `:TexAI 25 Add a TikZ diagram of a three-node directed cycle` inserts
  generated LaTeX before line 25 of the buffer you are editing.
- `:TexAI! 765 Look at the previous 30 lines and make a diagram for this` sends
  the whole file, with line numbers, so the prompt can point at any part of it.
- `:TexPreamble` summarises your `preamble.tex` once, so everything `:TexAI`
  writes matches the packages and macros you actually load.
- `:TexAIFix` reads your compiler's log, finds the line LaTeX complained about,
  and replaces it with a corrected version.
- `:TexAIMap a shortcut for an enumerate environment with the cursor after the
  first \item` drafts a Neovim key mapping into a file you review and save.

## Requirements

- macOS or Linux (Windows is not supported yet)
- Python 3.11 or newer
- Neovim 0.10 or newer, to open files and use `:TexAI`
- An OpenAI API key of your own, only for `:TexAI`

## Install

1. Check what you already have. You need Python 3.11 or newer and, to open
   files or use `:TexAI`, Neovim 0.10 or newer:

   ```sh
   python3 --version
   nvim --version | head -1
   ```

   Install whatever is missing or too old (macOS, with Homebrew):

   ```sh
   brew install python neovim
   ```

2. Install `pipx`, which keeps `texman` in its own environment while putting the
   command on your `PATH`:

   ```sh
   brew install pipx      # or: python3 -m pip install --user pipx
   pipx ensurepath
   ```

   `pipx ensurepath` adds `~/.local/bin` to your `PATH` if it isn't there
   already. Open a new terminal afterwards.

3. Install `texman` from a clone of this repository. `-e` installs it in
   editable mode, so `git pull` updates the command without reinstalling:

   ```sh
   cd /path/to/tex-manager
   pipx install -e .
   ```

4. Check that the command works from somewhere else entirely:

   ```sh
   cd ~ && texman --help
   ```

## Use

```sh
texman                      # open the catalog and scan in the background
texman scan                 # scan without the UI
texman --root ~/papers      # the UI, scanning one directory instead
texman scan --root ~/papers --root /usr/local/texlive   # two roots, no UI
```

By default both scan `~/Documents` and `~/Downloads`, which is where your own
documents live; scanning from `/` instead works, but buries them under tens of
thousands of TeX Live package files. `--root` replaces the defaults and can be
repeated. A default folder you do not have is skipped quietly; a folder you name
yourself has to exist, or the command says so and stops.

Cached results appear immediately and a full scan starts in the background.

| Key | Action |
| --- | --- |
| Arrow keys, Tab | Move the selection, switch panes |
| `j` / `k` | Move down / up; a count first repeats it (`5j`, `3k`, `4↓`) |
| `gg` / `G` | First / last row; `7G` goes to row 7 |
| `h` / `l` | Focus the directory pane / the file table |
| `;s` / `;a` | Jump to the next / previous directory (`2;s` skips one) |
| `/` | Filter by path or description (case-insensitive substring) |
| Enter | Open the selected file in Neovim |
| `d` | Edit the selected file's description |
| `n` | Create a new document from `preamble.tex` |
| `p` | Edit `preamble.tex`, creating it first if needed |
| `i` | Ignore the directory in context, or show it again |
| `r` | Start a full rescan |
| `s` | Stop the running scan |
| `q` | Quit |
| Escape | Cancel a dialog, a pending `;` or count, or leave the filter |

Counts and two-key sequences work the way they do in Vim: type the digits, then
the motion. A key that is not a motion drops the count and does its usual job,
and nothing here fires while you are typing in the filter or a dialog. In the
"All directories" view `;s` moves to the first file of the next directory group;
when a single directory is selected it moves the left pane to the next
directory instead, so the table follows.

The status line shows the current directory, how many files have been found, and
how many paths were skipped. Skipped paths are normally folders macOS protects —
including `~/Documents` and `~/Downloads` themselves, which macOS guards
separately. If a scan finds nothing at all, grant your terminal access when
macOS asks, or give it Full Disk Access in System Settings → Privacy & Security,
and press `r` to rescan. Unmounted drives cannot be scanned. A scan you stop is
incomplete, not failed: everything already found, and every description, is
kept.

### Ignoring directories you do not want to see

Press `i` to hide a directory: the one highlighted in the left pane, or the one
the selected file belongs to. It disappears from the catalog along with every
subdirectory beneath it, and scans stop entering it — useful for a
`node_modules` full of vendored `.tex` files, a finished course folder, or a
backup tree.

Nothing is deleted. Ignored directories stay listed at the bottom of the left
pane under `── ignored ──`, labelled with how many files they hide. Select one
to see exactly what it is hiding, and press `i` again to bring it back, with
every description exactly as you left it.

One entry covers a whole tree, so if you press `i` on a directory whose parent
is already ignored, texman says which parent hides it instead of adding a second
rule. A scan already in progress finishes with the rules it started with; press
`r` afterwards if you want it to take effect immediately.

The catalog lives in `${XDG_DATA_HOME:-~/.local/share}/texman/index.sqlite3`,
outside this repository.

### Starting a new document from your preamble

Press `p` once to create `preamble.tex` and open it in Neovim. It starts with a
plain `article` preamble; make it yours. It lives at
`${XDG_CONFIG_HOME:-~/.config}/texman/preamble.tex`, or wherever
`TEXMAN_PREAMBLE` or `--preamble PATH` points, and `p` never overwrites it once
it exists.

Press `n` to create a document. The dialog asks for a file name (`.tex` is added
if you leave it off) and a directory, prefilled with the directory of the file
under the cursor, or the highlighted directory when the left pane has focus.
Type another directory to put the file elsewhere; `~` works, and a directory
that does not exist yet is created. The new file is a copy of your preamble
followed by

```latex
\begin{document}

\end{document}
```

unless the preamble already contains `\begin{document}`, in which case it is
used verbatim as a whole skeleton. The document is catalogued immediately,
selected, and opened in Neovim. An existing file is never overwritten, and if
you have no `preamble.tex` yet the built-in default is used and the message
says so.

## Set up the AI helper

File management needs neither of these variables; `:TexAI` needs both.

1. Create an API key in your own OpenAI project, following the
   [API key setup instructions](https://developers.openai.com/api/docs/quickstart#create-and-export-an-api-key).

2. Export both variables in the shell you launch Neovim from, replacing both
   placeholders. `TEXMAN_OPENAI_MODEL` is an explicit text-generation model ID
   that your API project can use — a specific ID rather than a moving "latest"
   alias:

   ```sh
   export OPENAI_API_KEY="<your-own-api-key>"
   export TEXMAN_OPENAI_MODEL="<model-id-available-to-your-project>"
   export TEXMAN_OPENAI_MINI_MODEL="<small-model-id-available-to-your-project>"
   ```

   `TEXMAN_OPENAI_MINI_MODEL` is used for one small job — summarising your
   preamble, once — so a cheap model is the right choice. Without it `:TexAI`
   still works; it simply sends part of the document's own opening lines
   instead of the summary.

   To keep these settings, put them in your own shell configuration
   (`~/.zshrc`), never in a file tracked by this repository. API usage is billed
   to your own OpenAI account.

   Two optional variables tune how much of the document is sent:

   | Variable | Default | Effect |
   | --- | --- | --- |
   | `TEXMAN_WINDOW_LINES` | `10` | Lines sent each side of the target line |
   | `TEXMAN_FULL_FILE` | unset | `1` makes `:TexAI` behave like `:TexAI!` |

3. Copy the Lua module into your Neovim configuration:

   ```sh
   mkdir -p ~/.config/nvim/lua
   cp nvim/texman.lua ~/.config/nvim/lua/texman.lua
   ```

   Then add one line to your existing `~/.config/nvim/init.lua`, keeping the
   rest of your configuration exactly as it is:

   ```lua
   require('texman').setup()
   ```

   This is a plain module, not a plugin spec, so it works alongside a plugin
   manager such as lazy.nvim without being registered with it. If your
   configuration is `init.vim` rather than `init.lua`, use
   `lua require('texman').setup()` instead.

   If you already have a `:TexAI` command, `setup` says so and leaves it alone.
   Choose another uppercase name instead:

   ```lua
   require('texman').setup({ command = 'TexGen' })
   ```

   `fix_command`, `map_command`, and `preamble_command` rename `:TexAIFix`,
   `:TexAIMap`, and `:TexPreamble` the same way. `setup` also loads
   `~/.config/nvim/texman-keymaps.lua`, the file `:TexAIMap` writes to;
   `keymaps_file = '/some/other/path.lua'` moves it.

## Generating LaTeX

While editing a `.tex` or `.sty` file:

```vim
:TexAI 10 Add an aligned derivation of the quadratic formula
```

The fragment is inserted **before** line 10. Valid line numbers are `1` through
`N + 1` for an `N`-line buffer, where `N + 1` appends to the end.

- Your unsaved edits are part of the context sent with the prompt.
- The insertion is one undo step: press `u` to remove it.
- Nothing is written to disk until you `:write` yourself.
- If you change or close the buffer while the request is in flight, the result
  is discarded rather than inserted at a stale line, and you are asked to rerun.
- One request per buffer at a time. Errors (bad key, unavailable model, rate
  limit, timeout, no connection) are reported in a single short message; run the
  command again to retry.

### How much of the file is sent

Context lines are sent with their real line numbers, as `   765| text`, so your
prompt can refer to the document directly.

By default only a slice goes out: the document's opening lines for packages and
macros, and `TEXMAN_WINDOW_LINES` (10) lines on each side of the target.
Omitted stretches are labelled, so nothing looks contiguous that is not.

Add `!` to send the whole file instead:

```vim
:TexAI! 765 Look at the previous 30 lines and make a diagram for this
```

That is what makes a prompt like this one work — with the whole numbered file in
front of it, "the previous 30 lines" means lines 735 to 764. Very large
documents fall back to a wide window, and the notification tells you which shape
was used. Set `TEXMAN_FULL_FILE=1` if you always want the bang.

No other file in your catalog is read, and `\input` references are not followed.
Your shared `preamble.tex` is the one exception, and it is summarised once
rather than sent — see below.

## Matching your preamble

Everything `n` creates starts from one `preamble.tex`, so its packages and
macros are what generated LaTeX should match. Rather than send that file with
every request, texman summarises it once:

```vim
:TexPreamble
```

The summary is cached at `~/.local/share/texman/preamble-digest.json` and
travels with every later `:TexAI` and `:TexAIFix`. Edit `preamble.tex` and the
next request summarises it again; leave it alone and it costs nothing at all —
no file read, no extra request.

Running the command is optional: the first `:TexAI` after an edit does the same
work by itself. It is worth running by hand because it moves that cost out of
the way of a generation, and because it is the only place a misconfiguration is
reported — `:TexAI` deliberately stays quiet about a failed summary and simply
carries on without it.

- `:TexPreamble!` summarises again even when the cache is current.
- From a shell, `texman preamble --show` prints the summary it has.

## Fixing a compile error

Compile the document first — with vimtex that is `\ll` — then, in the buffer that
failed:

```vim
:TexAIFix
```

It reads the compiler's log, finds the first error blamed on *this* file, and
replaces that one line with a corrected version. You can add guidance:

```vim
:TexAIFix prefer \dfrac here
```

It finds the log the way your setup writes it: from vimtex's own build
information when vimtex is loaded, otherwise `<name>.log`, `build/<name>.log`, or
`out/<name>.log` beside the file.

LaTeX reports errors in several shapes, and all four are understood:

| What the log says | How the line is found |
| --- | --- |
| `./thesis.tex:25: Undefined control sequence.` | directly (vimtex passes `-file-line-error`) |
| `! Undefined control sequence.` then `l.25` | from the `l.25` marker |
| `! LaTeX Error: \begin{equation} on input line 6 ended by …` | from the line named in the message |
| `Runaway argument?` then `! File ended while scanning …` | by matching the source TeX echoed back — this is the usual shape of an unclosed brace, and the log names no line at all |

The rules are deliberately careful, because a wrong edit is worse than no edit:

- **The buffer must be saved.** Log line numbers describe the file on disk, so
  `:TexAIFix` refuses to work from an unsaved buffer rather than trust a stale
  line number. Save, recompile, then run it.
- **Only this file.** If the first error is in another file, it says which one and
  changes nothing; open that file and run the command there.
- **Only one line.** The blamed line is replaced, possibly by several lines when
  the fix needs them (a missing `\end{...}`, say). Nothing else is touched.
- **Never silently clean.** If the compile failed but the log names no line and
  nothing can be located, it says so and makes no edit — it will not tell you
  there are no errors when the build died.
- One undo step, and nothing is written to disk until you `:write`.

## Adding a key mapping

Describe the shortcut you want, in any TeX buffer or none:

```vim
:TexAIMap a keybind for \begin{itemize} with two \item lines and \end{itemize}, leaving the cursor after the first \item
```

The model drafts Lua for it, and texman opens `texman-keymaps.lua` in a split
with the draft appended, so you read the code before Neovim ever runs it. This
is the real draft that request produced:

```lua
-- a keybind for \begin{itemize} with two \item lines and \end{itemize}, leaving the cursor after the first \item
-- Add an insert-mode <Tab>e keybind for a LaTeX itemize environment with two \item lines, leaving the cursor after the first \item.
vim.api.nvim_create_autocmd('FileType', {
  pattern = { 'tex', 'plaintex' },
  group = 'texman_keymaps',
  callback = function(args)
    vim.keymap.set('i', '<Tab>e', '\\begin{itemize}<CR>\\item <CR>\\item <CR>\\end{itemize}<Up><Up><End>', {
      buffer = args.buf,
      desc = 'Insert itemize environment',
    })
  end,
})
```

Read it before you save: a draft is a suggestion, and cursor placement in
particular is worth checking. `:w` keeps it and makes it active at once; `u`
discards it. Name a key in the
request if you have one in mind (`... on <leader>e`); otherwise the model picks
one that is not already mapped, following the style of your existing mappings,
and says which in the comment.

Your `init.lua` is never edited. `require('texman').setup()` runs the mappings
file at startup and again every time you write it, inside `pcall`, so a mapping
that fails to load is reported instead of breaking Neovim. Lua that does not
compile is refused before anything is appended. The request sends only your
description, the current contents of that file, your leader keys, and the list
of keys already mapped (left-hand sides only) so the draft avoids them.

## Try the whole workflow

1. Run `texman` and wait for files to appear.
2. Select a file, press `d`, type a description, press Enter.
3. Press Enter to open it in Neovim.
4. Run `:TexPreamble` once, so what follows matches your own packages.
5. Try `:TexAI 10 Add an aligned derivation of the quadratic formula`, with a
   line number that exists in that buffer.
6. Press `u` to undo the insertion, or `:write` to keep it, then `:q` to return
   to the catalog.

## Tests

```sh
python -m unittest discover -s tests -t .       # 269 checks
nvim --headless -u NONE -l tests/test_nvim.lua  # 169 checks
```

Automated tests only scan a temporary fixture, never your whole machine, and the
OpenAI client is always stubbed, so they make no paid API calls. They also point
`XDG_DATA_HOME` at a temporary directory, so your own catalog and preamble
summary are never read or written.

### Verified by hand

- A full scan starting at `/` completed in 42 seconds on the development
  machine: 569,833 directories walked, 24,180 files catalogued, 557 paths
  skipped and reported, with the excluded roots (`/dev`, `/home`) named in the
  summary. Stopping a scan with Ctrl-C reported it as stopped, not complete.
  The default roots are a small fraction of that.
- `texman --help` works from a directory other than the repository.

### Verified with a live request

One real `:TexAI` request against a live OpenAI project, with
`TEXMAN_OPENAI_MODEL=gpt-5.6-luna`:

- `:TexAI 13 Add an aligned derivation of the quadratic formula` inserted 14
  lines immediately before line 13. The fragment was bare LaTeX — no Markdown
  fence, no prose — and used the `amsmath` environments already loaded in the
  document's preamble.
- Text above and below the insertion point was byte-identical afterwards,
  including an unsaved edit that was part of the context sent with the prompt.
- The file on disk was unchanged until `:write`, and changed only then.
- A single `u` removed the whole snippet and kept the unsaved edit.
- A second request in the same buffer worked, so the per-buffer request flag
  clears correctly.

The preamble summary and whole-file context (`:TexPreamble`, `:TexAI!`,
`TEXMAN_WINDOW_LINES`) are **not yet verified against a live project** — they are
covered by stubbed tests only, and mocked output never proves API connectivity.

`:TexAIFix` was verified against two real `latexmk` failures:

- An undefined control sequence (`./broken.tex:7: Undefined control sequence.`)
  was located from the log and the offending macro removed from line 7 only.
- An unclosed brace in `\frac{-b \pm \sqrt{b^2 - 4ac}{2a}`, which LaTeX reports
  as a fatal `File ended while scanning use of \frac` with **no line number**,
  was located through the runaway-argument text. The fix added the one missing
  brace, and the document then compiled to a PDF with no errors.

`:TexAIMap` was verified with two live requests, run through headless Neovim
against a scratch mappings file, with the same model:

- The first asked for `\begin{enumerate} \item \end{enumerate}` with the
  cursor after `\item`. The draft was a `tex`-only insert mapping on `<Tab>e`,
  in the same style as the `<Tab>l` mapping already in the author's
  `init.lua`; it compiled, was appended unsaved as one undoable change, and
  was active after `:w`. Typing it in a `tex` buffer inserted the right three
  lines, but its cursor move was `<Esc>2kA`, one line too far: the model had
  counted from below the snippet rather than from its last line. That is the
  kind of slip the review step exists for, and the instructions now spell out
  how to count cursor moves.
- The second, the itemize request shown above, was made after that change. Its
  draft used `<Up><Up><End>`, and typing `<Tab>e` in a `tex` buffer left the
  cursor exactly after the first `\item `.
