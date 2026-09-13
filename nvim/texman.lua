-- texman: generate and repair LaTeX from inside Neovim.
--
--   :TexAI <line> <prompt>   insert generated LaTeX before <line>
--   :TexAIFix [guidance]     replace the line the compiler blamed
--
-- Both Ex commands start with an uppercase letter as Neovim requires, and `/`
-- is left alone so ordinary search keeps working. Insertion and replacement
-- happen here; the `texman ai` helper only produces text.

local M = {}

local DEFAULT_COMMAND = 'TexAI'
local DEFAULT_FIX_COMMAND = 'TexAIFix'
local TEX_FILETYPES = { tex = true, plaintex = true, latex = true, context = true }
local TEX_SUFFIXES = { '.tex', '.sty', '.cls', '.ltx' }

-- The helper already caps its own API request at 60 seconds; this is a backstop
-- so a wedged process cannot block the buffer's next request forever.
local REQUEST_TIMEOUT_MS = 90000
-- Logs are normally tens of kilobytes; this only guards a pathological one.
local MAX_LOG_BYTES = 500000

-- One active request per buffer, keyed by buffer handle.
local active = {}

local function notify(message, level)
  vim.notify('texman: ' .. message, level or vim.log.levels.INFO)
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

--- Run one helper request for `buf`, applying the output on success.
---
--- `apply` receives the buffer and the helper's stdout and returns a message
--- describing what changed. It runs only after the buffer has been re-checked.
local function send(buf, payload, apply, progress)
  if active[buf] then
    notify('a request is already running for this buffer', vim.log.levels.WARN)
    return
  end

  local tick = vim.api.nvim_buf_get_changedtick(buf)

  local function finish(message, level)
    active[buf] = nil
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
        finish('the helper produced no LaTeX', vim.log.levels.ERROR)
        return
      end
      if not vim.api.nvim_buf_is_loaded(buf) then
        finish('the buffer was closed; rerun the command', vim.log.levels.WARN)
        return
      end
      if not vim.bo[buf].modifiable then
        finish('the buffer is no longer modifiable; rerun the command', vim.log.levels.WARN)
        return
      end
      if vim.api.nvim_buf_get_changedtick(buf) ~= tick then
        finish('the buffer changed since the request; rerun the command', vim.log.levels.WARN)
        return
      end
      finish(apply(buf, output))
    end)
  end

  active[buf] = true
  local ok, launch_error = pcall(vim.system, { 'texman', 'ai' }, {
    stdin = vim.json.encode(payload),
    text = true,
    timeout = REQUEST_TIMEOUT_MS,
  }, on_exit)
  if not ok then
    active[buf] = nil
    notify('could not run `texman ai`: ' .. tostring(launch_error), vim.log.levels.ERROR)
    return
  end
  notify(progress)
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

--- Insert generated LaTeX before line `L`; valid values are 1 through N + 1.
function M.request(args)
  local line, prompt, err = parse_args(args)
  if err then
    notify(err, vim.log.levels.ERROR)
    return
  end

  local buf = vim.api.nvim_get_current_buf()
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

  local payload = {
    mode = 'insert',
    line = line,
    prompt = prompt,
    -- In-memory lines, so unsaved edits are part of the context.
    buffer_lines = vim.api.nvim_buf_get_lines(buf, 0, -1, true),
  }

  local function apply(target, output)
    local lines = output_lines(output)
    break_undo(target)
    -- One call, so `u` undoes the whole insertion in one step.
    vim.api.nvim_buf_set_lines(target, line - 1, line - 1, true, lines)
    return string.format('inserted %d line(s) before line %d; press u to undo', #lines, line)
  end

  send(buf, payload, apply, string.format('generating LaTeX for line %d …', line))
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

  local function apply(target_buf, output)
    local replacement = output_lines(output)
    if #replacement == 1 and replacement[1] == original then
      return string.format('line %d already looks correct; nothing changed', entry.line)
    end
    break_undo(target_buf)
    -- One call, so `u` undoes the whole replacement in one step.
    vim.api.nvim_buf_set_lines(target_buf, entry.line - 1, entry.line, true, replacement)
    return string.format(
      'replaced line %d with %d line(s); press u to undo, :w to keep',
      entry.line,
      #replacement
    )
  end

  local assumed = entry.assumed and ' (the log named no file, assuming this one)' or ''
  send(
    buf,
    payload,
    apply,
    string.format('fixing line %d: %s%s', entry.line, entry.message, assumed)
  )
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

--- Register both Ex commands.
---
--- Pass `{ command = 'OtherName' }` or `{ fix_command = 'OtherName' }` if a name
--- is already taken.
function M.setup(opts)
  opts = opts or {}
  local name = opts.command or DEFAULT_COMMAND
  local fix_name = opts.fix_command or DEFAULT_FIX_COMMAND

  local ok = register(name, function(cmd)
    M.request(cmd.args)
  end, {
    nargs = '+',
    desc = 'Insert generated LaTeX before the given line',
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
  return true
end

return M
