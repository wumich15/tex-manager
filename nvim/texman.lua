-- texman: generate and repair LaTeX from inside Neovim.
--
--   :TexAI <line> <prompt>   insert generated LaTeX before <line>
--   :TexAI! <line> <prompt>  the same, with the whole file as context
--   :TexAIFix [guidance]     replace the line the compiler blamed
--   :TexAIMap <prompt>       draft a key mapping into texman's mappings file
--   :TexAIPrompt             put the last :TexAI command back on the command line
--   :TexPreamble             summarise the preamble template, once, and cache it
--
-- All Ex commands start with an uppercase letter as Neovim requires, and `/`
-- is left alone so ordinary search keeps working. Insertion and replacement
-- happen here; the `texman ai` helper only produces text.

local M = {}

local DEFAULT_COMMAND = 'TexAI'
local DEFAULT_FIX_COMMAND = 'TexAIFix'
local DEFAULT_MAP_COMMAND = 'TexAIMap'
local DEFAULT_PROMPT_COMMAND = 'TexAIPrompt'
local DEFAULT_PREAMBLE_COMMAND = 'TexPreamble'
-- Mappings drafted by :TexAIMap live in their own file, loaded by `setup()`
-- inside pcall, so a bad mapping can never break Neovim's startup. init.lua
-- itself is never edited.
local KEYMAPS_FILE_NAME = 'texman-keymaps.lua'
local KEYMAP_GROUP = 'texman_keymaps'
local KEYMAPS_HEADER = {
  '-- Key mappings drafted with :TexAIMap.',
  "-- require('texman').setup() runs this file at startup, and again each time it",
  '-- is written. Edit or delete anything here freely.',
}
-- Keys already taken are sent so the model avoids them; a handful of modes
-- and a cap keep that list short.
local MAPPED_MODES = { 'n', 'i', 'v' }
local MAX_MAPPED_KEYS = 300
local TEX_FILETYPES = { tex = true, plaintex = true, latex = true, context = true }
local TEX_SUFFIXES = { '.tex', '.sty', '.cls', '.ltx' }

-- The helper already caps its own API request at 60 seconds; this is a backstop
-- so a wedged process cannot block the buffer's next request forever.
local REQUEST_TIMEOUT_MS = 90000
-- Logs are normally tens of kilobytes; this only guards a pathological one.
local MAX_LOG_BYTES = 500000

-- One active request per target, keyed by buffer handle, or by one of these
-- for the targets that are not buffers.
local active = {}
local KEYMAP_KEY = 'keymap'
local PREAMBLE_KEY = 'preamble'
local BUSY_MESSAGE = {
  [KEYMAP_KEY] = 'a mapping request is already running',
  [PREAMBLE_KEY] = 'the preamble is already being summarised',
}

local uv = vim.uv or vim.loop

-- Extmarks in this namespace track where a result will land and show the
-- in-buffer progress indicator; nothing else is ever stored there.
local NAMESPACE = vim.api.nvim_create_namespace('texman')
local SPINNER = { '⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏' }
local SPINNER_MS = 120
-- Inserted or replaced lines are highlighted this long, so a result that
-- arrives while the user is looking elsewhere is still noticed.
local FLASH_MS = 1500
local MAX_PROMPT_SHOWN = 60

-- Messages wait while the user is typing on the command line or answering a
-- prompt. A message echoed at that moment scrolls the screen, and Neovim then
-- repeats the half-typed command on a new line at every keystroke; a message
-- during a hit-enter prompt swallows the next key. Deferred messages are
-- delivered from this queue as soon as the mode is safe again.
local pending_messages = {}
local flush_timer = nil
local FLUSH_MS = 100

local function unsafe_mode()
  local mode = vim.api.nvim_get_mode()
  local first = string.sub(mode.mode, 1, 1)
  return mode.blocking or first == 'c' or first == 'r'
end

--- Reduce `text` to one line that fits the command line.
---
--- A message wider than the screen wraps and ends in a "Press ENTER" prompt,
--- which interrupts whatever the user was typing; a truncated line does not.
local function fit(text)
  text = string.gsub(text, '%s*\n.*$', '')
  local width = math.max(20, vim.o.columns - 1)
  if vim.fn.strdisplaywidth(text) <= width then
    return text
  end
  local kept = vim.fn.strcharpart(text, 0, width - 1)
  while vim.fn.strchars(kept) > 0 and vim.fn.strdisplaywidth(kept .. '…') > width do
    kept = vim.fn.strcharpart(kept, 0, vim.fn.strchars(kept) - 1)
  end
  return kept .. '…'
end

local function flush_messages()
  if unsafe_mode() then
    return false
  end
  local queued = pending_messages
  pending_messages = {}
  for _, entry in ipairs(queued) do
    vim.notify(entry.message, entry.level)
  end
  return true
end

local function notify(message, level)
  message = fit('texman: ' .. message)
  level = level or vim.log.levels.INFO
  if not unsafe_mode() then
    vim.notify(message, level)
    return
  end
  table.insert(pending_messages, { message = message, level = level })
  if not flush_timer then
    flush_timer = uv.new_timer()
    flush_timer:start(FLUSH_MS, FLUSH_MS, vim.schedule_wrap(function()
      if flush_timer and flush_messages() then
        flush_timer:stop()
        flush_timer:close()
        flush_timer = nil
      end
    end))
  end
end

--- Shorten a prompt for display beside the spinner.
local function shown(prompt)
  if vim.fn.strchars(prompt) <= MAX_PROMPT_SHOWN then
    return prompt
  end
  return vim.fn.strcharpart(prompt, 0, MAX_PROMPT_SHOWN - 1) .. '…'
end

local function first_line(text)
  if not text or text == '' then
    return nil
  end
  local line = vim.split(text, '\n', { plain = true })[1]
  line = vim.trim(line or '')
  return line ~= '' and line or nil
end

local function has_tex_suffix(path)
  local lowered = string.lower(path)
  for _, suffix in ipairs(TEX_SUFFIXES) do
    if string.sub(lowered, -#suffix) == suffix then
      return true
    end
  end
  return false
end

local function is_tex_buffer(buf)
  if TEX_FILETYPES[vim.bo[buf].filetype] then
    return true
  end
  return has_tex_suffix(vim.api.nvim_buf_get_name(buf))
end

local function buffer_problem(buf)
  if not vim.api.nvim_buf_is_loaded(buf) then
    return 'the buffer is no longer loaded'
  end
  if not vim.bo[buf].modifiable then
    return 'the buffer is not modifiable'
  end
  if not is_tex_buffer(buf) then
    return 'this is not a TeX buffer (expected filetype tex, or a .tex/.sty file)'
  end
  return nil
end

--- Strip the single trailing newline the helper writes, keeping everything else.
local function output_lines(output)
  local text = string.gsub(output, '\r\n', '\n')
  text = string.gsub(text, '\n$', '')
  return vim.split(text, '\n', { plain = true })
end

local function break_undo(buf)
  -- Setting 'undolevels' syncs undo, so the edit starts its own undo block
  -- instead of merging into the user's previous change. Writing the value back
  -- unchanged keeps the buffer's existing setting.
  vim.bo[buf].undolevels = vim.bo[buf].undolevels
end

-- ---------------------------------------------------------------- requests

--- Run one `texman` subcommand, handing its output to `on_output` on success.
---
--- `key` limits concurrency: one request per buffer, per mappings file, or per
--- preamble. `stdin` is the text to write to the process, or nil for none.
--- `on_output` receives stdout and returns a message describing what changed,
--- plus an optional log level. `empty` is the message for a command that
--- printed nothing.
local function run(key, argv, stdin, on_output, progress, empty, cleanup)
  if active[key] then
    notify(BUSY_MESSAGE[key] or 'a request is already running for this buffer', vim.log.levels.WARN)
    if cleanup then
      pcall(cleanup)
    end
    return false
  end

  local function finish(message, level)
    active[key] = nil
    if cleanup then
      pcall(cleanup)
    end
    if message then
      notify(message, level)
    end
  end

  local function on_exit(result)
    vim.schedule(function()
      if result.code ~= 0 then
        local message = first_line(result.stderr)
        if not message then
          if result.signal ~= nil and result.signal ~= 0 then
            message = string.format(
              'the helper was stopped after %d seconds; run the command again',
              REQUEST_TIMEOUT_MS / 1000
            )
          else
            message = 'helper exited with status ' .. tostring(result.code)
          end
        end
        finish(message, vim.log.levels.ERROR)
        return
      end
      local output = result.stdout or ''
      if string.match(output, '^%s*$') then
        finish(empty or 'the helper produced no LaTeX', vim.log.levels.ERROR)
        return
      end
      finish(on_output(output))
    end)
  end

  active[key] = true
  local ok, launch_error = pcall(vim.system, argv, {
    stdin = stdin,
    text = true,
    timeout = REQUEST_TIMEOUT_MS,
  }, on_exit)
  if not ok then
    finish(
      'could not run `' .. table.concat(argv, ' ') .. '`: ' .. tostring(launch_error),
      vim.log.levels.ERROR
    )
    return false
  end
  notify(progress)
  return true
end

--- Send one JSON request to `texman ai`, which is how every generation works.
local function send(key, payload, on_output, progress, empty, cleanup)
  return run(key, { 'texman', 'ai' }, vim.json.encode(payload), on_output, progress, empty, cleanup)
end

--- Wrap `apply(buf, output)` so it runs only if `buf` can still take the edit.
---
--- Where the edit lands is the tracker's business (see `track`), so the user
--- may keep typing while a request runs; only a closed or read-only buffer
--- discards the result.
local function guarded(buf, apply)
  return function(output)
    if not vim.api.nvim_buf_is_loaded(buf) then
      return 'the buffer was closed; rerun the command', vim.log.levels.WARN
    end
    if not vim.bo[buf].modifiable then
      return 'the buffer is no longer modifiable; rerun the command', vim.log.levels.WARN
    end
    return apply(buf, output)
  end
end

--- Mark where a result will land, and show a spinner there until it arrives.
---
--- The extmark does two jobs. It follows the target line through the user's
--- own edits, so typing elsewhere while a request runs moves the insertion
--- point instead of invalidating the result. And it carries the indicator: a
--- virtual line above the insertion point (below the last line when
--- appending), or text after the line being repaired, with a spinner, the
--- seconds elapsed, and what was asked. `opts.label` names the work,
--- `opts.detail` is the prompt or message, `opts.below` puts the line under the
--- marked row, and `opts.eol` draws at the end of the line instead.
---
--- `row()` gives the current 0-based row, or nil if the mark is gone.
--- `stop()` removes the indicator; it is safe to call more than once.
local function track(buf, row, opts)
  local mark = vim.api.nvim_buf_set_extmark(buf, NAMESPACE, row, 0, {})
  local started = uv.now()
  local frame = 0
  local timer = uv.new_timer()
  local tracker = {}

  local function position()
    if not vim.api.nvim_buf_is_loaded(buf) then
      return nil
    end
    local found = vim.api.nvim_buf_get_extmark_by_id(buf, NAMESPACE, mark, {})
    if #found == 0 then
      return nil
    end
    return found
  end

  local function decorate()
    local at = position()
    if not at then
      return false
    end
    frame = frame % #SPINNER + 1
    local elapsed = math.floor((uv.now() - started) / 1000)
    local head = string.format('%s texman: %s … %ds', SPINNER[frame], opts.label, elapsed)
    local chunks = { { ' ' .. head, 'DiagnosticInfo' }, { '  ' .. (opts.detail or ''), 'Comment' } }
    local extmark = { id = mark }
    if opts.eol then
      extmark.virt_text = chunks
      extmark.virt_text_pos = 'eol'
    else
      chunks[1][1] = ' ' .. chunks[1][1]
      extmark.virt_lines = { chunks }
      extmark.virt_lines_above = not opts.below
    end
    return pcall(vim.api.nvim_buf_set_extmark, buf, NAMESPACE, at[1], at[2], extmark)
  end

  function tracker.row()
    local at = position()
    return at and at[1] or nil
  end

  function tracker.stop()
    if timer then
      timer:stop()
      timer:close()
      timer = nil
    end
    pcall(vim.api.nvim_buf_del_extmark, buf, NAMESPACE, mark)
  end

  decorate()
  timer:start(SPINNER_MS, SPINNER_MS, vim.schedule_wrap(function()
    if timer and not decorate() then
      tracker.stop()
    end
  end))
  return tracker
end

--- Highlight `count` lines from 0-based `row` for a moment, then let go.
local function flash(buf, row, count)
  if count < 1 then
    return
  end
  local ok, mark = pcall(vim.api.nvim_buf_set_extmark, buf, NAMESPACE, row, 0, {
    end_row = row + count,
    end_col = 0,
    hl_group = 'DiffAdd',
    hl_eol = true,
    strict = false,
  })
  if not ok then
    return
  end
  local timer = uv.new_timer()
  timer:start(FLASH_MS, 0, vim.schedule_wrap(function()
    timer:close()
    pcall(vim.api.nvim_buf_del_extmark, buf, NAMESPACE, mark)
  end))
end

-- -------------------------------------------------------- :TexAI (insert)

--- Split "<line> <prompt>", keeping spaces and backslashes in the prompt.
local function parse_args(args)
  local number, prompt = string.match(args or '', '^%s*(%d+)%s+(.*)$')
  if not number then
    return nil, nil, 'usage: :' .. (M.command_name or DEFAULT_COMMAND) .. ' <line> <prompt>'
  end
  if string.match(prompt, '^%s*$') then
    return nil, nil, 'the prompt is empty'
  end
  return tonumber(number), prompt, nil
end

-- The last `:TexAI` request per buffer, and the last one anywhere, so the
-- prompt can be brought back after a failure or a refusal (see `M.recall`).
local last = {}

--- Insert generated LaTeX before line `L`; valid values are 1 through N + 1.
---
--- With `bang` (`:TexAI!`) the whole buffer goes out as context, numbered, so
--- the prompt may refer to any part of it -- "look at the previous 30 lines".
--- Without it only the lines around `L` do. `TEXMAN_FULL_FILE=1` makes the
--- bang the default for someone who always wants it.
---
--- While the request runs, a spinner line marks the insertion point in the
--- buffer and follows it through the user's edits; typing elsewhere in the
--- meantime is fine. The result is inserted before the line that was line `L`
--- when the command ran, wherever it is by then.
function M.request(args, bang)
  local line, prompt, err = parse_args(args)
  if err then
    notify(err, vim.log.levels.ERROR)
    return
  end

  local buf = vim.api.nvim_get_current_buf()
  -- Recorded before any refusal, so a rejected prompt can be recalled too.
  last[buf] = { line = line, prompt = prompt, bang = bang == true }
  M.last_request = last[buf]

  local problem = buffer_problem(buf)
  if problem then
    notify(problem, vim.log.levels.ERROR)
    return
  end
  local count = vim.api.nvim_buf_line_count(buf)
  if line < 1 or line > count + 1 then
    notify(
      string.format('line %d is out of range; valid lines are 1 to %d', line, count + 1),
      vim.log.levels.ERROR
    )
    return
  end

  local full = bang == true or vim.env.TEXMAN_FULL_FILE == '1'
  local payload = {
    mode = 'insert',
    line = line,
    prompt = prompt,
    full = full,
    -- In-memory lines, so unsaved edits are part of the context.
    buffer_lines = vim.api.nvim_buf_get_lines(buf, 0, -1, true),
  }

  -- Appending marks the last line and inserts after it; everything else marks
  -- line L and inserts before it. Either way the mark moves with the text.
  local append = line == count + 1
  local tracker = track(buf, append and count - 1 or line - 1, {
    label = full and string.format('generating LaTeX from the whole file (%d lines)', count)
      or 'generating LaTeX',
    detail = shown(prompt),
    below = append,
  })

  local function apply(target, output)
    local row = tracker.row()
    if row == nil then
      return 'the insertion point was lost; rerun the command', vim.log.levels.WARN
    end
    if append then
      row = row + 1
    end
    local lines = output_lines(output)
    break_undo(target)
    -- One call, so `u` undoes the whole insertion in one step.
    vim.api.nvim_buf_set_lines(target, row, row, true, lines)
    flash(target, row, #lines)
    return string.format('inserted %d line(s) before line %d; press u to undo', #lines, row + 1)
  end

  send(
    buf,
    payload,
    guarded(buf, apply),
    string.format(
      'generating LaTeX for line %d (%s) …',
      line,
      full and string.format('whole file, %d lines', count) or 'nearby lines'
    ),
    nil,
    tracker.stop
  )
end

--- The `:TexAI` command line that would repeat the last request, or nil.
---
--- The request for `buf` (default: the current buffer) is preferred, then the
--- last one made anywhere. Only an explicit `!` is repeated; a bang that came
--- from `TEXMAN_FULL_FILE` still applies without being spelled out.
function M.recall_command(buf)
  local entry = last[buf or vim.api.nvim_get_current_buf()] or M.last_request
  if not entry then
    return nil
  end
  return string.format(
    '%s%s %d %s',
    M.command_name or DEFAULT_COMMAND,
    entry.bang and '!' or '',
    entry.line,
    entry.prompt
  )
end

--- Put the last `:TexAI` command back on the command line, unexecuted.
---
--- The prompt is there to edit, and Enter sends it again -- after a failure,
--- a refusal, or an `u` that removed a first attempt. Returns false when
--- nothing has been asked yet.
function M.recall()
  local command = M.recall_command()
  if not command then
    notify('nothing to recall: no :' .. (M.command_name or DEFAULT_COMMAND) .. ' request has been made yet', vim.log.levels.WARN)
    return false
  end
  vim.api.nvim_feedkeys(':' .. command, 'n', false)
  return true
end

-- ------------------------------------------------------- :TexAIFix (repair)

--- Resolve a path the compiler reported, which may be relative to the log.
local function absolute(path, base)
  if string.sub(path, 1, 1) == '/' then
    return vim.fn.resolve(path)
  end
  local joined = base .. '/' .. string.gsub(path, '^%./', '')
  return vim.fn.resolve(vim.fn.fnamemodify(joined, ':p'))
end

--- Where the compiler left its log. vimtex knows; otherwise guess conventionally.
local function find_log(buf)
  local candidates = {}
  local ok, root = pcall(vim.fn.eval, 'b:vimtex.root')
  local ok_info, info = pcall(vim.fn.eval, 'b:vimtex.compiler.file_info')
  if ok and ok_info and type(root) == 'string' and type(info) == 'table' and info.jobname then
    local directory = root
    local ok_out, out_dir = pcall(vim.fn.eval, 'b:vimtex.compiler.out_dir')
    if ok_out and type(out_dir) == 'string' and out_dir ~= '' then
      directory = string.sub(out_dir, 1, 1) == '/' and out_dir or (root .. '/' .. out_dir)
    end
    table.insert(candidates, directory .. '/' .. info.jobname .. '.log')
  end

  local name = vim.api.nvim_buf_get_name(buf)
  local directory = vim.fn.fnamemodify(name, ':h')
  local stem = vim.fn.fnamemodify(name, ':t:r')
  table.insert(candidates, directory .. '/' .. stem .. '.log')
  table.insert(candidates, directory .. '/build/' .. stem .. '.log')
  table.insert(candidates, directory .. '/out/' .. stem .. '.log')

  for _, candidate in ipairs(candidates) do
    if vim.fn.filereadable(candidate) == 1 then
      return candidate, candidates
    end
  end
  return nil, candidates
end

--- Collapse whitespace, so TeX's re-printed source can be matched to a line.
local function normalize(text)
  return (string.gsub(text, '%s+', ''))
end

--- Find the buffer line holding the text TeX said it ran away with.
---
--- A runaway argument is how an unclosed brace or environment usually shows up,
--- and TeX names no line for it because the file ended while it was still
--- scanning. It does echo the source it swallowed, with its own spacing, so the
--- line is found by comparing with whitespace removed.
local function locate_text(buffer_lines, text)
  local needle = normalize(text)
  if #needle < 8 then
    return nil
  end
  for index, line in ipairs(buffer_lines) do
    if string.find(normalize(line), needle, 1, true) then
      return index
    end
  end
  -- The swallowed text may span several lines; its start is enough.
  local prefix = string.sub(needle, 1, 20)
  if #prefix < 8 then
    return nil
  end
  for index, line in ipairs(buffer_lines) do
    if string.find(normalize(line), prefix, 1, true) then
      return index
    end
  end
  return nil
end

--- Find the first error the compiler blamed on `target`.
---
--- Four forms are recognised, in order of how precisely they name a line:
---
---  1. `file:line: message`, from `-file-line-error` (vimtex passes it).
---  2. `! message` followed by `l.<n>`, which plain TeX prints.
---  3. `! message` mentioning `on input line <n>`, as an unclosed environment does.
---  4. `! message` with no line at all, located through the `Runaway argument?`
---     text that precedes it -- the usual shape of an unclosed brace.
---
--- Forms 2 to 4 name no file, so they are only trusted when the log blames no
--- file anywhere. Returns the entry, or nil plus a reason to report.
local function scan_log(lines, target, base, buffer_lines)
  local other_file, pending, classic, on_input, runaway = nil, nil, nil, nil, nil
  local unattributed, collecting = nil, false

  for _, text in ipairs(lines) do
    local path, number, message = string.match(text, '^(.-):(%d+):%s*(.+)$')
    if path and has_tex_suffix(path) then
      if absolute(path, base) == target then
        return { line = tonumber(number), message = message }
      elseif not other_file then
        other_file = path
      end
    end

    if string.match(text, '^Runaway argument') and not runaway then
      collecting, runaway = true, ''
    elseif collecting then
      if string.match(text, '^!') then
        collecting = false
      else
        runaway = runaway .. text
      end
    end

    local bang = string.match(text, '^!%s*(.+)$')
    if bang then
      pending = bang
      if not unattributed then
        unattributed = bang
      end
      local reported = string.match(bang, 'on input line (%d+)')
      if reported and not on_input then
        on_input = { line = tonumber(reported), message = bang, assumed = true }
      end
    end

    local blamed = string.match(text, '^l%.(%d+)')
    if blamed and pending and not classic then
      classic = { line = tonumber(blamed), message = pending, assumed = true }
    end
  end

  if other_file then
    return nil, { kind = 'other_file', detail = other_file }
  end
  if classic then
    return classic
  end
  if on_input then
    return on_input
  end
  if runaway and runaway ~= '' and unattributed then
    local located = locate_text(buffer_lines, runaway)
    if located then
      return { line = located, message = unattributed, assumed = true }
    end
  end
  if unattributed then
    -- The build failed but nothing points at a line; say so rather than
    -- claiming the log is clean.
    return nil, { kind = 'unattributed', detail = unattributed }
  end
  return nil, { kind = 'none' }
end

--- Replace the line the compiler blamed with a corrected version.
function M.fix(args)
  local buf = vim.api.nvim_get_current_buf()
  local problem = buffer_problem(buf)
  if problem then
    notify(problem, vim.log.levels.ERROR)
    return
  end
  if vim.bo[buf].modified then
    notify(
      'save the buffer and recompile first: the compiler log describes the file on disk',
      vim.log.levels.ERROR
    )
    return
  end

  local log, candidates = find_log(buf)
  if not log then
    notify(
      'no compiler log found (looked for '
        .. table.concat(candidates, ', ')
        .. '); compile the document first, for example with vimtex\'s \\ll',
      vim.log.levels.ERROR
    )
    return
  end

  local ok, lines = pcall(vim.fn.readfile, log)
  if not ok or type(lines) ~= 'table' then
    notify('could not read the compiler log at ' .. log, vim.log.levels.ERROR)
    return
  end

  local buffer_lines = vim.api.nvim_buf_get_lines(buf, 0, -1, true)
  local target = vim.fn.resolve(vim.fn.fnamemodify(vim.api.nvim_buf_get_name(buf), ':p'))
  local entry, reason = scan_log(lines, target, vim.fn.fnamemodify(log, ':h'), buffer_lines)
  if not entry then
    reason = reason or { kind = 'none' }
    if reason.kind == 'other_file' then
      notify(
        'the first error in ' .. log .. ' is in ' .. reason.detail
          .. '; open that file and run the command there',
        vim.log.levels.WARN
      )
    elseif reason.kind == 'unattributed' then
      notify(
        'the compile failed but the log names no line: ' .. reason.detail
          .. ' -- this is usually an unclosed brace or environment earlier in the file',
        vim.log.levels.ERROR
      )
    else
      notify(
        vim.fn.fnamemodify(log, ':t') .. ' reports no errors for this buffer',
        vim.log.levels.INFO
      )
    end
    return
  end

  local count = vim.api.nvim_buf_line_count(buf)
  if entry.line < 1 or entry.line > count then
    notify(
      string.format(
        'the log blames line %d but the buffer has %d line(s); recompile and try again',
        entry.line,
        count
      ),
      vim.log.levels.ERROR
    )
    return
  end

  local log_text = table.concat(lines, '\n')
  if #log_text > MAX_LOG_BYTES then
    log_text = string.sub(log_text, -MAX_LOG_BYTES)
  end

  local prompt = entry.message
  local extra = vim.trim(args or '')
  if extra ~= '' then
    prompt = prompt .. ' | Additional guidance from the author: ' .. extra
  end

  local payload = {
    mode = 'fix',
    line = entry.line,
    prompt = prompt,
    buffer_lines = buffer_lines,
    log_text = log_text,
  }

  local original = vim.api.nvim_buf_get_lines(buf, entry.line - 1, entry.line, true)[1]
  local tracker = track(buf, entry.line - 1, {
    label = 'fixing this line',
    detail = shown(entry.message),
    eol = true,
  })

  local function apply(target_buf, output)
    local row = tracker.row()
    -- The mark follows the blamed line through edits elsewhere, but the line
    -- itself must still read as it did when the compiler saw it.
    if row == nil or vim.api.nvim_buf_get_lines(target_buf, row, row + 1, true)[1] ~= original then
      return string.format(
        'line %d changed since the request; save, recompile, and rerun the command',
        entry.line
      ), vim.log.levels.WARN
    end
    local replacement = output_lines(output)
    if #replacement == 1 and replacement[1] == original then
      return string.format('line %d already looks correct; nothing changed', row + 1)
    end
    break_undo(target_buf)
    -- One call, so `u` undoes the whole replacement in one step.
    vim.api.nvim_buf_set_lines(target_buf, row, row + 1, true, replacement)
    flash(target_buf, row, #replacement)
    return string.format(
      'replaced line %d with %d line(s); press u to undo, :w to keep',
      row + 1,
      #replacement
    )
  end

  local assumed = entry.assumed and ' (the log named no file, assuming this one)' or ''
  send(
    buf,
    payload,
    guarded(buf, apply),
    string.format('fixing line %d: %s%s', entry.line, entry.message, assumed),
    nil,
    tracker.stop
  )
end

-- ------------------------------------------------------ :TexAIMap (mappings)

local function keymaps_path()
  return M.keymaps_file or (vim.fn.stdpath('config') .. '/' .. KEYMAPS_FILE_NAME)
end

--- Run the mappings file, if there is one. Returns true unless it failed.
---
--- The autocmd group the file's mappings use is cleared first, so running the
--- file again after an edit does not leave stale autocmds behind.
function M.load_keymaps()
  local path = keymaps_path()
  if vim.fn.filereadable(path) == 0 then
    return true
  end
  vim.api.nvim_create_augroup(KEYMAP_GROUP, { clear = true })
  local ok, err = pcall(dofile, path)
  if not ok then
    notify(
      'could not load ' .. path .. ': ' .. tostring(err) .. ' -- fix the file and write it again',
      vim.log.levels.ERROR
    )
    return false
  end
  return true
end

--- The keys already mapped, as "mode lhs", so the helper can avoid them.
local function mapped_keys()
  local keys, seen = {}, {}
  local function add(mode, maps)
    for _, map in ipairs(maps) do
      local entry = mode .. ' ' .. map.lhs
      if not seen[entry] and #keys < MAX_MAPPED_KEYS then
        seen[entry] = true
        table.insert(keys, entry)
      end
    end
  end
  for _, mode in ipairs(MAPPED_MODES) do
    add(mode, vim.api.nvim_get_keymap(mode))
    add(mode, vim.api.nvim_buf_get_keymap(0, mode))
  end
  return keys
end

--- Compile Lua without running it; a snippet that does not parse is refused.
local function compiles(code)
  local loader = loadstring or load
  local chunk, err = loader(code, '=texman keymap')
  if chunk then
    return true
  end
  return false, err
end

--- Draft a mapping from a description and append it to the mappings file.
---
--- The file is opened in a split with the new lines appended as one undoable
--- change, and nothing is written: `:w` keeps the mapping and loads it, `u`
--- discards it. That way the user reads the code before Neovim ever runs it.
function M.map(args)
  local prompt = vim.trim(args or '')
  if prompt == '' then
    notify(
      'usage: :' .. (M.map_command_name or DEFAULT_MAP_COMMAND) .. ' <what the shortcut should do>',
      vim.log.levels.ERROR
    )
    return
  end

  local path = keymaps_path()
  local existing = ''
  if vim.fn.filereadable(path) == 1 then
    existing = table.concat(vim.fn.readfile(path), '\n')
  end

  local payload = {
    mode = 'keymap',
    prompt = prompt,
    keymap_file_text = existing,
    mapleader = vim.g.mapleader or '',
    maplocalleader = vim.g.maplocalleader or '',
    mapped_keys = mapped_keys(),
  }

  local function on_output(output)
    local code = table.concat(output_lines(output), '\n')
    local ok, err = compiles(code)
    if not ok then
      return 'the generated Lua does not compile, so nothing was added ('
        .. tostring(err) .. '); rerun the command',
        vim.log.levels.ERROR
    end

    local opened, open_err = pcall(vim.cmd, 'split ' .. vim.fn.fnameescape(path))
    if not opened then
      return 'could not open ' .. path .. ': ' .. tostring(open_err), vim.log.levels.ERROR
    end
    local buf = vim.api.nvim_get_current_buf()
    if not vim.bo[buf].modifiable then
      return path .. ' is not modifiable', vim.log.levels.ERROR
    end

    local lines = { '-- ' .. prompt }
    vim.list_extend(lines, vim.split(code, '\n', { plain = true }))
    local count = vim.api.nvim_buf_line_count(buf)
    local current = vim.api.nvim_buf_get_lines(buf, 0, -1, true)
    local start = count
    if count == 1 and current[1] == '' then
      -- A new or empty file: begin with the header rather than a blank line.
      start = 0
      local with_header = vim.deepcopy(KEYMAPS_HEADER)
      table.insert(with_header, '')
      vim.list_extend(with_header, lines)
      lines = with_header
    else
      table.insert(lines, 1, '')
    end
    break_undo(buf)
    -- One call, so `u` removes the whole draft in one step.
    vim.api.nvim_buf_set_lines(buf, start, start == 0 and -1 or start, true, lines)
    vim.api.nvim_win_set_cursor(0, { start + (start == 0 and #KEYMAPS_HEADER + 2 or 2), 0 })
    return string.format(
      'appended %d line(s) to %s; review them, then :w to keep and activate the mapping, or u to discard',
      #lines,
      vim.fn.fnamemodify(path, ':t')
    )
  end

  send(KEYMAP_KEY, payload, on_output, 'drafting a key mapping …', 'the helper produced no Lua')
end

-- ------------------------------------------------- :TexPreamble (digest)

--- Summarise the preamble template once, so later requests can match it.
---
--- The summary is cached on disk against the preamble's contents, so this is
--- the only time the file is read or sent: `:TexAI` inlines the cached summary
--- and makes no extra request. Running it by hand is optional -- the first
--- `:TexAI` after an edit does the same thing -- but it moves the cost out of
--- the way of a generation, and it is where a misconfiguration is reported.
--- With `!` the summary is made again even when it is still current.
function M.preamble(bang)
  local argv = { 'texman', 'preamble' }
  if bang then
    table.insert(argv, '--force')
  end
  run(PREAMBLE_KEY, argv, nil, function(output)
    return first_line(output) or 'the preamble summary is up to date'
  end, 'summarising the preamble …', 'texman printed nothing')
end

-- ------------------------------------------------------------------- setup

local function register(name, handler, description)
  if not string.match(name, '^%u') then
    notify('command name must start with an uppercase letter: ' .. name, vim.log.levels.ERROR)
    return false
  end
  if vim.api.nvim_get_commands({})[name] then
    notify(
      string.format(
        ':%s already exists; pass another name to require("texman").setup()',
        name
      ),
      vim.log.levels.ERROR
    )
    return false
  end
  vim.api.nvim_create_user_command(name, handler, description)
  return true
end

--- Register the Ex commands and load the mappings file.
---
--- Pass `{ command = 'OtherName' }`, `{ fix_command = 'OtherName' }`,
--- `{ map_command = 'OtherName' }`, `{ prompt_command = 'OtherName' }`, or
--- `{ preamble_command = 'OtherName' }` if a name is already taken, and
--- `{ keymaps_file = '/path/to/file.lua' }` to keep drafted mappings elsewhere
--- than `stdpath('config')/texman-keymaps.lua`.
function M.setup(opts)
  opts = opts or {}
  local name = opts.command or DEFAULT_COMMAND
  local fix_name = opts.fix_command or DEFAULT_FIX_COMMAND
  local map_name = opts.map_command or DEFAULT_MAP_COMMAND
  local prompt_name = opts.prompt_command or DEFAULT_PROMPT_COMMAND
  local preamble_name = opts.preamble_command or DEFAULT_PREAMBLE_COMMAND

  local ok = register(name, function(cmd)
    M.request(cmd.args, cmd.bang)
  end, {
    nargs = '+',
    bang = true,
    desc = 'Insert generated LaTeX before the given line (! sends the whole file)',
  })
  if not ok then
    return false
  end
  M.command_name = name

  -- `:TexAIFix` is optional: losing it must not cost the user `:TexAI`.
  if register(fix_name, function(cmd)
    M.fix(cmd.args)
  end, {
    nargs = '*',
    desc = 'Replace the line the LaTeX compiler blamed with a corrected version',
  }) then
    M.fix_command_name = fix_name
  end

  if opts.keymaps_file then
    M.keymaps_file = vim.fn.fnamemodify(vim.fn.expand(opts.keymaps_file), ':p')
  end
  if register(map_name, function(cmd)
    M.map(cmd.args)
  end, {
    nargs = '+',
    desc = 'Draft a key mapping from a description into the texman mappings file',
  }) then
    M.map_command_name = map_name
  end

  -- Optional too: the command history still holds every prompt without it.
  if register(prompt_name, function()
    M.recall()
  end, {
    nargs = 0,
    desc = 'Put the last :TexAI command back on the command line to edit and resend',
  }) then
    M.prompt_command_name = prompt_name
  end

  -- Also optional: `:TexAI` seeds the summary by itself, so losing the command
  -- costs only the ability to refresh it on purpose.
  if register(preamble_name, function(cmd)
    M.preamble(cmd.bang)
  end, {
    nargs = 0,
    bang = true,
    desc = 'Summarise the preamble template so :TexAI matches its packages and macros',
  }) then
    M.preamble_command_name = preamble_name
  end

  -- Writing the mappings file is how a drafted mapping is accepted, so that is
  -- when it takes effect. Comparing resolved paths avoids autocmd-pattern
  -- escaping for unusual characters in the config path.
  local group = vim.api.nvim_create_augroup('texman', { clear = true })
  vim.api.nvim_create_autocmd('BufWritePost', {
    group = group,
    callback = function(event)
      local written = vim.fn.resolve(vim.fn.fnamemodify(event.file, ':p'))
      if written == vim.fn.resolve(keymaps_path()) and M.load_keymaps() then
        notify('mappings from ' .. vim.fn.fnamemodify(written, ':t') .. ' are active')
      end
    end,
  })
  M.load_keymaps()
  return true
end

return M
