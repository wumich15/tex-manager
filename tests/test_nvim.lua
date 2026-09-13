-- Headless check for nvim/texman.lua.
--
-- Run with:  nvim --headless -u NONE -l tests/test_nvim.lua
--
-- A fake `texman` executable stands in for the real helper, so this check
-- makes no API calls. It covers insertion before the first and a middle line,
-- appending, invalid lines, unsaved context, one-step undo, switched buffers,
-- changed and closed buffers, and helper failure.

local script_dir = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ':p:h')
local module_path = script_dir .. '/../nvim/texman.lua'

local tmp = vim.fn.tempname()
vim.fn.mkdir(tmp, 'p')
local capture = tmp .. '/request.json'

-- ------------------------------------------------------------------ fake helper

local fake = tmp .. '/texman'
local fake_source = [==[#!/bin/sh
payload=$(cat)
if [ -n "$TEXMAN_FAKE_CAPTURE" ]; then
  printf '%s' "$payload" > "$TEXMAN_FAKE_CAPTURE"
fi
case "${TEXMAN_FAKE_MODE:-ok}" in
  ok)
    cat <<'SNIPPET'
\begin{equation}
  x = 1
\end{equation}
SNIPPET
    ;;
  fenced)
    printf '%s\n' '```latex' '\beta' '```'
    ;;
  slow)
    sleep 1
    printf '%s\n' '\alpha'
    ;;
  empty)
    exit 0
    ;;
  fail)
    echo "texman ai: OPENAI_API_KEY is not set; export your own OpenAI API key" >&2
    exit 1
    ;;
esac
]==]
local handle = assert(io.open(fake, 'w'))
handle:write(fake_source)
handle:close()
vim.fn.setfperm(fake, 'rwxr-xr-x')
vim.env.PATH = tmp .. ':' .. vim.env.PATH
vim.env.TEXMAN_FAKE_MODE = 'ok'

-- ------------------------------------------------------------------- harness

local failures = {}

local function ok(name, condition, detail)
  if condition then
    print('ok   - ' .. name)
  else
    table.insert(failures, name)
    print('FAIL - ' .. name .. (detail and ('  :: ' .. detail) or ''))
  end
end

local function eq(name, got, want)
  local same = vim.deep_equal(got, want)
  ok(name, same, same and nil or ('got ' .. vim.inspect(got) .. ' want ' .. vim.inspect(want)))
end

local notes = {}
vim.notify = function(message, level)
  table.insert(notes, { message = message, level = level })
end

local function wait_for(pattern)
  local found = vim.wait(8000, function()
    for _, note in ipairs(notes) do
      if string.match(note.message, pattern) then
        return true
      end
    end
    return false
  end, 20)
  return found
end

local function last_note()
  local note = notes[#notes]
  return note and note.message or '<none>'
end

local counter = 0
local function new_tex_buffer(lines, opts)
  opts = opts or {}
  counter = counter + 1
  vim.cmd('enew')
  local buf = vim.api.nvim_get_current_buf()
  vim.api.nvim_buf_set_name(buf, tmp .. '/doc' .. counter .. (opts.extension or '.tex'))
  vim.bo[buf].filetype = opts.filetype or 'tex'
  vim.api.nvim_buf_set_lines(buf, 0, -1, true, lines)
  return buf
end

local texman = dofile(module_path)
local sample = { '\\documentclass{article}', '\\begin{document}', 'Hello.', '\\end{document}' }
local snippet = { '\\begin{equation}', '  x = 1', '\\end{equation}' }

local function run(args)
  notes = {}
  texman.request(args)
end

-- --------------------------------------------------------------------- setup

ok('setup registers :TexAI', texman.setup() == true)
eq('command name recorded', texman.command_name, 'TexAI')
ok('command exists', vim.api.nvim_get_commands({})['TexAI'] ~= nil)

notes = {}
ok('duplicate setup reports the conflict', texman.setup() == false)
ok('conflict message suggests another name', string.match(last_note(), 'already exists') ~= nil,
  last_note())

ok('alternate command name can be chosen', texman.setup({ command = 'TexAIAlt' }) == true)
ok('alternate command registered', vim.api.nvim_get_commands({})['TexAIAlt'] ~= nil)

notes = {}
ok('lowercase command name refused', texman.setup({ command = 'texai' }) == false)

ok('normal search is untouched', vim.fn.maparg('/', 'n') == '')

-- ------------------------------------------------------------------ insertion

do
  local buf = new_tex_buffer(sample)
  run('1 add an equation')
  ok('progress is reported', string.match(last_note(), 'generating LaTeX') ~= nil, last_note())
  ok('insertion before line 1 completes', wait_for('inserted 3 line'), last_note())
  local want = vim.list_extend(vim.deepcopy(snippet), sample)
  eq('inserted before the first line', vim.api.nvim_buf_get_lines(buf, 0, -1, true), want)
end

do
  local buf = new_tex_buffer(sample)
  run('3 add an equation')
  ok('insertion before a middle line completes', wait_for('inserted 3 line'), last_note())
  local want = { sample[1], sample[2] }
  vim.list_extend(want, snippet)
  vim.list_extend(want, { sample[3], sample[4] })
  eq('inserted before the middle line', vim.api.nvim_buf_get_lines(buf, 0, -1, true), want)
end

do
  local buf = new_tex_buffer(sample)
  run('5 append an equation')
  ok('append at N + 1 completes', wait_for('inserted 3 line'), last_note())
  local want = vim.deepcopy(sample)
  vim.list_extend(want, snippet)
  eq('appended at the end of the buffer', vim.api.nvim_buf_get_lines(buf, 0, -1, true), want)
end

do
  local buf = new_tex_buffer(sample)
  vim.env.TEXMAN_FAKE_MODE = 'fenced'
  run('1 add beta')
  -- Fence removal is the Python helper's job; Lua inserts what it is given.
  ok('helper output is inserted unmodified', wait_for('inserted 3 line'), last_note())
  eq('helper output is inserted verbatim',
    vim.api.nvim_buf_get_lines(buf, 0, 3, true),
    { '```latex', '\\beta', '```' })
  vim.env.TEXMAN_FAKE_MODE = 'ok'
end

-- ------------------------------------------------------------- invalid input

for _, case in ipairs({
  { args = '6 too far', label = 'line beyond N + 1' },
  { args = '0 too small', label = 'line zero' },
  { args = '2', label = 'missing prompt' },
  { args = '2    ', label = 'blank prompt' },
  { args = 'two words', label = 'non-numeric line' },
  { args = '', label = 'no arguments' },
}) do
  local buf = new_tex_buffer(sample)
  vim.fn.delete(capture)
  vim.env.TEXMAN_FAKE_CAPTURE = capture
  run(case.args)
  vim.wait(150)
  vim.env.TEXMAN_FAKE_CAPTURE = nil
  eq('buffer unchanged for ' .. case.label,
    vim.api.nvim_buf_get_lines(buf, 0, -1, true), sample)
  ok('no helper call for ' .. case.label, vim.fn.filereadable(capture) == 0)
end

do
  local buf = new_tex_buffer(sample, { filetype = 'markdown', extension = '.md' })
  run('1 add an equation')
  vim.wait(150)
  ok('non-TeX buffer is refused', string.match(last_note(), 'not a TeX buffer') ~= nil, last_note())
  eq('non-TeX buffer unchanged', vim.api.nvim_buf_get_lines(buf, 0, -1, true), sample)
end

do
  local buf = new_tex_buffer(sample)
  vim.bo[buf].modifiable = false
  run('1 add an equation')
  vim.wait(150)
  ok('non-modifiable buffer is refused', string.match(last_note(), 'not modifiable') ~= nil,
    last_note())
  vim.bo[buf].modifiable = true
end

do
  -- A .tex file with no filetype set still counts as a TeX buffer.
  local buf = new_tex_buffer(sample, { filetype = '' })
  run('1 add an equation')
  ok('.tex name is enough without a filetype', wait_for('inserted 3 line'), last_note())
  eq('inserted into the unset-filetype buffer',
    vim.api.nvim_buf_get_lines(buf, 0, 3, true), snippet)
end

-- ----------------------------------------------------------- unsaved context

do
  local buf = new_tex_buffer(sample)
  vim.api.nvim_buf_set_lines(buf, 2, 3, true, { 'Unsaved edit.' })
  vim.fn.delete(capture)
  vim.env.TEXMAN_FAKE_CAPTURE = capture
  run('2 use my unsaved text')
  ok('request with unsaved edits completes', wait_for('inserted 3 line'), last_note())
  vim.env.TEXMAN_FAKE_CAPTURE = nil
  local payload = vim.json.decode(table.concat(vim.fn.readfile(capture), '\n'))
  eq('line is sent as given', payload.line, 2)
  eq('prompt is preserved', payload.prompt, 'use my unsaved text')
  eq('unsaved buffer text is sent', payload.buffer_lines[3], 'Unsaved edit.')
  ok('buffer was never written to disk', vim.fn.filereadable(vim.api.nvim_buf_get_name(buf)) == 0)
end

do
  local buf = new_tex_buffer(sample)
  vim.fn.delete(capture)
  vim.env.TEXMAN_FAKE_CAPTURE = capture
  run([[2 draw \tikz{a} with  two spaces and $x \le y$]])
  ok('prompt with backslashes completes', wait_for('inserted 3 line'), last_note())
  vim.env.TEXMAN_FAKE_CAPTURE = nil
  local payload = vim.json.decode(table.concat(vim.fn.readfile(capture), '\n'))
  eq('spaces and backslashes survive', payload.prompt,
    [[draw \tikz{a} with  two spaces and $x \le y$]])
end

-- ------------------------------------------------------------------ undo

do
  local buf = new_tex_buffer(sample)
  -- An edit of the user's own, which one `u` must not discard.
  vim.api.nvim_buf_set_lines(buf, 2, 3, true, { 'Hello again.' })
  local before = vim.api.nvim_buf_get_lines(buf, 0, -1, true)
  run('3 add an equation')
  ok('insertion before undo completes', wait_for('inserted 3 line'), last_note())
  vim.api.nvim_set_current_buf(buf)
  vim.cmd('silent undo')
  eq('one undo step removes only the insertion',
    vim.api.nvim_buf_get_lines(buf, 0, -1, true), before)
end

-- -------------------------------------------------- switched / changed buffers

do
  vim.env.TEXMAN_FAKE_MODE = 'slow'
  local target = new_tex_buffer(sample)
  run('1 add an equation')
  local other = new_tex_buffer({ 'other buffer' })
  ok('insertion survives a window switch', wait_for('inserted 1 line'), last_note())
  eq('original buffer received the snippet',
    vim.api.nvim_buf_get_lines(target, 0, 1, true), { '\\alpha' })
  eq('the other buffer was untouched',
    vim.api.nvim_buf_get_lines(other, 0, -1, true), { 'other buffer' })
  vim.env.TEXMAN_FAKE_MODE = 'ok'
end

do
  vim.env.TEXMAN_FAKE_MODE = 'slow'
  local buf = new_tex_buffer(sample)
  run('1 add an equation')
  vim.api.nvim_buf_set_lines(buf, 0, 0, true, { 'Typed while waiting.' })
  ok('changed buffer is reported', wait_for('changed since the request'), last_note())
  local lines = vim.api.nvim_buf_get_lines(buf, 0, -1, true)
  eq('stale result was discarded', lines[1], 'Typed while waiting.')
  ok('no snippet was inserted', lines[2] == sample[1], vim.inspect(lines))
  vim.env.TEXMAN_FAKE_MODE = 'ok'
end

do
  vim.env.TEXMAN_FAKE_MODE = 'slow'
  local buf = new_tex_buffer(sample)
  run('1 add an equation')
  vim.cmd('enew')
  vim.api.nvim_buf_delete(buf, { force = true })
  ok('closed buffer is reported', wait_for('closed'), last_note())
  ok('buffer really is gone', not vim.api.nvim_buf_is_loaded(buf))
  vim.env.TEXMAN_FAKE_MODE = 'ok'
end

do
  vim.env.TEXMAN_FAKE_MODE = 'slow'
  local buf = new_tex_buffer(sample)
  run('1 first request')
  notes = {}
  texman.request('1 second request')
  ok('a second request for the same buffer is refused',
    string.match(last_note(), 'already running') ~= nil, last_note())
  ok('first request still completes', wait_for('inserted 1 line'), last_note())
  eq('only one snippet was inserted',
    vim.api.nvim_buf_get_lines(buf, 0, 2, true), { '\\alpha', sample[1] })
  -- The flag must be cleared, so a later request works.
  vim.env.TEXMAN_FAKE_MODE = 'ok'
  run('1 third request')
  ok('the buffer accepts a later request', wait_for('inserted 3 line'), last_note())
end

-- ------------------------------------------------------------ helper failure

do
  vim.env.TEXMAN_FAKE_MODE = 'fail'
  local buf = new_tex_buffer(sample)
  run('2 add an equation')
  ok('helper failure is reported', wait_for('OPENAI_API_KEY'), last_note())
  eq('buffer unchanged after failure',
    vim.api.nvim_buf_get_lines(buf, 0, -1, true), sample)
  -- The active flag must be cleared on the failure path too.
  vim.env.TEXMAN_FAKE_MODE = 'ok'
  run('2 retry')
  ok('retry works after a failure', wait_for('inserted 3 line'), last_note())
end

do
  vim.env.TEXMAN_FAKE_MODE = 'empty'
  local buf = new_tex_buffer(sample)
  run('2 add an equation')
  ok('empty output is reported', wait_for('no LaTeX'), last_note())
  eq('buffer unchanged after empty output',
    vim.api.nvim_buf_get_lines(buf, 0, -1, true), sample)
  vim.env.TEXMAN_FAKE_MODE = 'ok'
end

do
  -- With no `texman` on PATH the command must fail cleanly.
  local saved = vim.env.PATH
  vim.env.PATH = tmp .. '/nowhere'
  local buf = new_tex_buffer(sample)
  run('1 add an equation')
  vim.wait(300)
  ok('missing helper is reported', string.match(last_note(), 'could not run') ~= nil, last_note())
  eq('buffer unchanged when the helper is missing',
    vim.api.nvim_buf_get_lines(buf, 0, -1, true), sample)
  vim.env.PATH = saved
end

-- ------------------------------------------------------------------ summary

vim.fn.delete(tmp, 'rf')
if #failures == 0 then
  print('\nall checks passed')
  os.exit(0)
end
print('\n' .. #failures .. ' check(s) failed: ' .. table.concat(failures, ', '))
os.exit(1)
