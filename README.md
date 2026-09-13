# tex-manager

`texman` is a personal file manager for LaTeX files, with AI help for diagrams
and other hard-to-TeX stuff.

It scans your computer for `.tex` and `.sty` files and shows them in one
searchable terminal catalog, grouped by directory. You can describe any file in
your own words, open it in Neovim, and — while editing — ask for a generated
LaTeX fragment with `:TexAI <line> <prompt>`.

Your files are never moved or copied, so relative `\input`, `\include`, image,
and style references keep working. The catalog and your descriptions stay on
your machine. Nothing is uploaded except the prompts you explicitly ask for.

See [ARCHITECTURE.md](ARCHITECTURE.md) for how it works internally.

## What it does

- One catalog of every `.tex` and `.sty` file the current user can read,
  including hidden folders, project directories, `/Volumes`, and installed TeX
  trees.
- A description per file that survives restarts, rescans, stopped scans, and
  files that go missing.
- Enter opens the selected file in Neovim, in its own directory, and returns you
  to the same place in the catalog.
- `:TexAI 25 Add a TikZ diagram of a three-node directed cycle` inserts
  generated LaTeX before line 25 of the buffer you are editing.

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
texman scan                 # scan without the UI, starting at /
texman scan --root ~/papers # a smaller scan, for a quick try
```

Cached results appear immediately and a full scan starts in the background.

| Key | Action |
| --- | --- |
| Arrow keys, Tab | Move the selection, switch panes |
| `/` | Filter by path or description (case-insensitive substring) |
| Enter | Open the selected file in Neovim |
| `d` | Edit the selected file's description |
| `r` | Start a full rescan |
| `s` | Stop the running scan |
| `q` | Quit |
| Escape | Cancel a dialog, or leave the filter |

The status line shows the current directory, how many files have been found, and
how many paths were skipped. Skipped paths are normally folders macOS protects.
To cover more of the disk, grant your terminal Full Disk Access in System
Settings → Privacy & Security and press `r` to rescan. Unmounted drives cannot
be scanned. A scan you stop is incomplete, not failed: everything already found,
and every description, is kept.

The catalog lives in `${XDG_DATA_HOME:-~/.local/share}/texman/index.sqlite3`,
outside this repository.

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
   ```

   To keep these settings, put them in your own shell configuration
   (`~/.zshrc`), never in a file tracked by this repository. API usage is billed
   to your own OpenAI account.

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

Only the prompt and a bounded slice of the current buffer are sent: up to the
first 100 lines for packages and macros, and up to 40 lines on each side of the
insertion point. No other file in your catalog is read, and `\input` references
are not followed.

## Try the whole workflow

1. Run `texman` and wait for files to appear.
2. Select a file, press `d`, type a description, press Enter.
3. Press Enter to open it in Neovim.
4. Try `:TexAI 10 Add an aligned derivation of the quadratic formula`, with a
   line number that exists in that buffer.
5. Press `u` to undo the insertion, or `:write` to keep it, then `:q` to return
   to the catalog.

## Tests

```sh
python -m unittest discover -s tests -t .     # 101 checks
nvim --headless -u NONE -l tests/test_nvim.lua  # 58 checks
```

Automated tests only scan a temporary fixture, never your whole machine, and the
OpenAI client is always stubbed, so they make no paid API calls.

### Verified by hand

- A full scan starting at `/` completed in 42 seconds on the development
  machine: 569,833 directories walked, 24,180 files catalogued, 557 paths
  skipped and reported, with the excluded roots (`/dev`, `/home`) named in the
  summary. Stopping a scan with Ctrl-C reported it as stopped, not complete.
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
