-- texman: :TexAI <line> <prompt> inserts generated LaTeX before <line>.
--
-- The Ex command starts with an uppercase letter as Neovim requires, and `/`
-- is left alone so ordinary search keeps working. Insertion happens here;
-- the `texman ai` helper only produces text.

local M = {}

local DEFAULT_COMMAND = 'TexAI'
local TEX_FILETYPES = { tex = true, plaintex = true, latex = true, context = true }
-- The helper already caps its own API request at 60 seconds; this is a backstop
-- so a wedged process cannot block the buffer's next request forever.
local REQUEST_TIMEOUT_MS = 90000

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

local function is_tex_buffer(buf)
  if TEX_FILETYPES[vim.bo[buf].filetype] then
    return true
  end
  local name = vim.api.nvim_buf_get_name(buf)
  return string.match(string.lower(name), '%.tex$') ~= nil
    or string.match(string.lower(name), '%.sty$') ~= nil
end

--- Insert before line `L`; valid values are 1 through N + 1, where N + 1 appends.
local function check_request(buf, line)
  if not vim.api.nvim_buf_is_loaded(buf) then
    return 'the buffer is no longer loaded'
  end
  if not vim.bo[buf].modifiable then
    return 'the buffer is not modifiable'
  end
  if not is_tex_buffer(buf) then
    return 'this is not a TeX buffer (expected filetype tex, or a .tex/.sty file)'
  end
  local count = vim.api.nvim_buf_line_count(buf)
  if line < 1 or line > count + 1 then
    return string.format('line %d is out of range; valid lines are 1 to %d', line, count + 1)
  end
  return nil
end

local function insert_snippet(buf, line, output)
  -- Strip the single trailing newline the helper writes, then keep every
  -- other character, including LaTeX backslashes and blank lines.
  local text = string.gsub(output, '\r\n', '\n')
  text = string.gsub(text, '\n$', '')
  local lines = vim.split(text, '\n', { plain = true })
  -- Setting 'undolevels' syncs undo, so the insertion starts its own undo
  -- block instead of merging into the user's previous edit. Writing the value
  -- back unchanged keeps the buffer's existing setting.
  vim.bo[buf].undolevels = vim.bo[buf].undolevels
  -- One call, so `u` undoes the whole insertion in one step.
  vim.api.nvim_buf_set_lines(buf, line - 1, line - 1, true, lines)
  return #lines
end

--- Run one generation request for the current buffer.
function M.request(args)
  local line, prompt, err = parse_args(args)
  if err then
    notify(err, vim.log.levels.ERROR)
    return
  end

  local buf = vim.api.nvim_get_current_buf()
  local problem = check_request(buf, line)
  if problem then
    notify(problem, vim.log.levels.ERROR)
    return
  end
  if active[buf] then
    notify('a request is already running for this buffer', vim.log.levels.WARN)
    return
  end

  -- In-memory lines, so unsaved edits are part of the context.
  local buffer_lines = vim.api.nvim_buf_get_lines(buf, 0, -1, true)
  local tick = vim.api.nvim_buf_get_changedtick(buf)
  local payload = vim.json.encode({
    line = line,
    prompt = prompt,
    buffer_lines = buffer_lines,
  })

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
      local inserted = insert_snippet(buf, line, output)
      finish(string.format('inserted %d line(s) before line %d; press u to undo', inserted, line))
    end)
  end

  active[buf] = true
  local ok, launch_error = pcall(vim.system, { 'texman', 'ai' }, {
    stdin = payload,
    text = true,
    timeout = REQUEST_TIMEOUT_MS,
  }, on_exit)
  if not ok then
    active[buf] = nil
    notify('could not run `texman ai`: ' .. tostring(launch_error), vim.log.levels.ERROR)
    return
  end
  notify(string.format('generating LaTeX for line %d …', line))
end

--- Register the Ex command. Pass { command = 'OtherName' } on a conflict.
function M.setup(opts)
  opts = opts or {}
  local name = opts.command or DEFAULT_COMMAND
  if not string.match(name, '^%u') then
    notify('command name must start with an uppercase letter: ' .. name, vim.log.levels.ERROR)
    return false
  end
  if vim.api.nvim_get_commands({})[name] then
    notify(
      string.format(
        ':%s already exists; call require("texman").setup({ command = "OtherName" })',
        name
      ),
      vim.log.levels.ERROR
    )
    return false
  end
  vim.api.nvim_create_user_command(name, function(cmd)
    M.request(cmd.args)
  end, {
    nargs = '+',
    desc = 'Insert generated LaTeX before the given line',
  })
  M.command_name = name
  return true
end

return M
