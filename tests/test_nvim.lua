-- Headless check for nvim/texman.lua.
--
-- Run with:  nvim --headless -u NONE -l tests/test_nvim.lua
--
-- A fake `texman` executable stands in for the real helper, so this check
-- makes no API calls. It covers insertion before the first and a middle line,
-- appending, invalid lines, unsaved context, one-step undo, switched buffers,
-- changed and closed buffers, helper failure, log-driven fixes, and drafting
-- key mappings into the mappings file, whole-file context, and the cached
-- preamble summary.

local script_dir = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ':p:h')
local module_path = script_dir .. '/../nvim/texman.lua'

local tmp = vim.fn.tempname()
vim.fn.mkdir(tmp, 'p')
local capture = tmp .. '/request.json'
local argv_capture = tmp .. '/argv.txt'

-- ------------------------------------------------------------------ fake helper

local fake = tmp .. '/texman'
local fake_source = [==[#!/bin/sh
if [ -n "$TEXMAN_FAKE_ARGV" ]; then
  printf '%s' "$*" > "$TEXMAN_FAKE_ARGV"
fi
if [ "$1" = "preamble" ]; then
  case "${TEXMAN_FAKE_PREAMBLE:-ok}" in
    ok)
      echo "~/.config/texman/preamble.tex: summarised (4 lines, mini-x, cached in ~/.local/share/texman/preamble-digest.json)"
      ;;
    slow)
      sleep 1
      echo "~/.config/texman/preamble.tex: summarised (4 lines, mini-x)"
      ;;
    empty)
      ;;
    fail)
      echo "texman preamble: TEXMAN_OPENAI_MINI_MODEL is not set" >&2
      exit 1
      ;;
  esac
  exit 0
fi
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
  fixed)
    printf '%s\n' 'Corrected \emph{line} from the helper.'
    ;;
  fixed_multi)
    printf '%s\n' 'First corrected line.' '\end{equation}'
    ;;
  unchanged)
    printf '%s\n' 'Blamed line.'
    ;;
  slow)
    sleep 1
    printf '%s\n' '\alpha'
    ;;
  keymap)
    printf '%s\n' "vim.keymap.set('n', '<leader>zz', function() vim.notify('texman greeting') end, { desc = 'texman test mapping' })"
    ;;
  keymap_slow)
    sleep 1
    printf '%s\n' "vim.keymap.set('n', '<leader>zz', function() vim.notify('texman greeting') end, { desc = 'texman test mapping' })"
    ;;
  keymap_bad)
    printf '%s\n' "vim.keymap.set('n' <<< not lua"
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

--- Write `lines` to a real file and edit it, so the buffer is not modified.
local function open_saved_file(name, lines)
  local path = tmp .. '/' .. name
  vim.fn.writefile(lines, path)
  vim.cmd('edit! ' .. vim.fn.fnameescape(path))
  local buf = vim.api.nvim_get_current_buf()
  vim.bo[buf].filetype = 'tex'
  return buf, path
end

--- Write a log next to `path`, in the shape latexmk -file-line-error produces.
local function write_log(path, body)
  local log = (path:gsub('%.tex$', '.log'))
  vim.fn.writefile(body, log)
  return log
end

local texman = dofile(module_path)
local keymaps_file = tmp .. '/texman-keymaps.lua'
local keymap_snippet = "vim.keymap.set('n', '<leader>zz', function() vim.notify('texman greeting') end, { desc = 'texman test mapping' })"
local sample = { '\\documentclass{article}', '\\begin{document}', 'Hello.', '\\end{document}' }
local snippet = { '\\begin{equation}', '  x = 1', '\\end{equation}' }

local function run(args, bang)
  notes = {}
  texman.request(args, bang)
end

--- Read back the JSON the fake helper captured.
local function captured()
  return vim.json.decode(table.concat(vim.fn.readfile(capture), '\n'))
end

-- --------------------------------------------------------------------- setup

ok('setup registers :TexAI', texman.setup({ keymaps_file = keymaps_file }) == true)
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

-- ------------------------------------------------------------- :TexAIFix

ok('fix command registered', vim.api.nvim_get_commands({})['TexAIFix'] ~= nil)
eq('fix command name recorded', texman.fix_command_name, 'TexAIFix')

local blamed = {
  '\\documentclass{article}',
  '\\begin{document}',
  'Blamed line.',
  '\\end{document}',
}

local function error_log(file, line, message)
  return {
    'This is pdfTeX, Version 3.141592653',
    'LaTeX Font Info:    Checking defaults on input line 4.',
    '',
    './' .. file .. ':' .. line .. ': ' .. message,
    'l.' .. line .. ' Blamed line.',
    'The control sequence at the end of the top line',
    'Output written on doc.pdf (1 page).',
  }
end

do
  vim.env.TEXMAN_FAKE_MODE = 'fixed'
  local buf, path = open_saved_file('fix1.tex', blamed)
  write_log(path, error_log('fix1.tex', 3, 'Undefined control sequence.'))
  vim.fn.delete(capture)
  vim.env.TEXMAN_FAKE_CAPTURE = capture
  notes = {}
  texman.fix('')
  ok('progress names the blamed line', string.match(last_note(), 'fixing line 3') ~= nil, last_note())
  ok('fix completes', wait_for('replaced line 3'), last_note())
  vim.env.TEXMAN_FAKE_CAPTURE = nil
  eq('the blamed line was replaced', vim.api.nvim_buf_get_lines(buf, 0, -1, true), {
    blamed[1], blamed[2], 'Corrected \\emph{line} from the helper.', blamed[4],
  })
  local payload = vim.json.decode(table.concat(vim.fn.readfile(capture), '\n'))
  eq('mode is fix', payload.mode, 'fix')
  eq('the blamed line number is sent', payload.line, 3)
  ok('the compiler message is the prompt',
    string.match(payload.prompt, 'Undefined control sequence') ~= nil, payload.prompt)
  ok('the log is sent', string.match(payload.log_text, 'fix1%.tex:3:') ~= nil)
  ok('the buffer lines are sent', #payload.buffer_lines == 4)
  -- One undo must put the original line back.
  vim.api.nvim_set_current_buf(buf)
  vim.cmd('silent undo')
  eq('one undo restores the blamed line', vim.api.nvim_buf_get_lines(buf, 0, -1, true), blamed)
  ok('the file on disk is untouched until :write', vim.deep_equal(vim.fn.readfile(path), blamed))
end

do
  vim.env.TEXMAN_FAKE_MODE = 'fixed_multi'
  local buf, path = open_saved_file('fix2.tex', blamed)
  write_log(path, error_log('fix2.tex', 3, 'Missing \\end{equation} inserted.'))
  notes = {}
  texman.fix('')
  ok('multi-line replacement completes', wait_for('replaced line 3 with 2 line'), last_note())
  eq('replacement can add lines', vim.api.nvim_buf_get_lines(buf, 0, -1, true), {
    blamed[1], blamed[2], 'First corrected line.', '\\end{equation}', blamed[4],
  })
end

do
  vim.env.TEXMAN_FAKE_MODE = 'unchanged'
  local buf, path = open_saved_file('fix3.tex', blamed)
  write_log(path, error_log('fix3.tex', 3, 'Undefined control sequence.'))
  notes = {}
  texman.fix('')
  ok('an unchanged answer is reported', wait_for('already looks correct'), last_note())
  eq('buffer untouched when nothing changed',
    vim.api.nvim_buf_get_lines(buf, 0, -1, true), blamed)
end

do
  vim.env.TEXMAN_FAKE_MODE = 'fixed'
  local buf, path = open_saved_file('fix4.tex', blamed)
  write_log(path, error_log('fix4.tex', 3, 'Undefined control sequence.'))
  vim.fn.delete(capture)
  vim.env.TEXMAN_FAKE_CAPTURE = capture
  notes = {}
  texman.fix('prefer \\textbf over \\bf')
  ok('extra guidance still fixes', wait_for('replaced line 3'), last_note())
  vim.env.TEXMAN_FAKE_CAPTURE = nil
  local payload = vim.json.decode(table.concat(vim.fn.readfile(capture), '\n'))
  ok('extra guidance reaches the prompt',
    string.find(payload.prompt, 'Additional guidance', 1, true) ~= nil
      and string.find(payload.prompt, 'textbf over', 1, true) ~= nil, payload.prompt)
end

do
  -- An unsaved buffer must be refused: log line numbers describe the disk file.
  local buf, path = open_saved_file('fix5.tex', blamed)
  write_log(path, error_log('fix5.tex', 3, 'Undefined control sequence.'))
  vim.api.nvim_buf_set_lines(buf, 0, 0, true, { '% a new unsaved line' })
  vim.fn.delete(capture)
  vim.env.TEXMAN_FAKE_CAPTURE = capture
  notes = {}
  texman.fix('')
  vim.wait(200)
  vim.env.TEXMAN_FAKE_CAPTURE = nil
  ok('modified buffer is refused', string.match(last_note(), 'save the buffer') ~= nil, last_note())
  ok('no helper call for a modified buffer', vim.fn.filereadable(capture) == 0)
  vim.cmd('silent edit!')
end

do
  local buf, path = open_saved_file('fix6.tex', blamed)
  -- No log at all.
  notes = {}
  texman.fix('')
  vim.wait(200)
  ok('missing log is reported', string.match(last_note(), 'no compiler log found') ~= nil, last_note())
  ok('missing log names where it looked', string.match(last_note(), 'fix6%.log') ~= nil, last_note())
  eq('buffer unchanged without a log', vim.api.nvim_buf_get_lines(buf, 0, -1, true), blamed)
end

do
  local buf, path = open_saved_file('fix7.tex', blamed)
  write_log(path, {
    'This is pdfTeX, Version 3.141592653',
    'Output written on doc.pdf (1 page).',
  })
  notes = {}
  texman.fix('')
  vim.wait(200)
  ok('a clean log says so', string.match(last_note(), 'no errors for this buffer') ~= nil, last_note())
  eq('buffer unchanged for a clean log',
    vim.api.nvim_buf_get_lines(buf, 0, -1, true), blamed)
end

do
  -- The first error belongs to another file: never edit this buffer blindly.
  local buf, path = open_saved_file('fix8.tex', blamed)
  write_log(path, error_log('chapter-two.tex', 12, 'Undefined control sequence.'))
  vim.fn.delete(capture)
  vim.env.TEXMAN_FAKE_CAPTURE = capture
  notes = {}
  texman.fix('')
  vim.wait(200)
  vim.env.TEXMAN_FAKE_CAPTURE = nil
  ok('an error in another file is reported',
    string.match(last_note(), 'chapter%-two%.tex') ~= nil, last_note())
  ok('no helper call for another file\'s error', vim.fn.filereadable(capture) == 0)
  eq('buffer unchanged for another file\'s error',
    vim.api.nvim_buf_get_lines(buf, 0, -1, true), blamed)
end

do
  -- A log blaming a line past the end of the buffer must not be applied.
  local buf, path = open_saved_file('fix9.tex', blamed)
  write_log(path, error_log('fix9.tex', 99, 'Undefined control sequence.'))
  notes = {}
  texman.fix('')
  vim.wait(200)
  ok('an out-of-range log line is refused',
    string.match(last_note(), 'blames line 99') ~= nil, last_note())
  eq('buffer unchanged for an out-of-range line',
    vim.api.nvim_buf_get_lines(buf, 0, -1, true), blamed)
end

do
  -- Without -file-line-error TeX names no file; the classic form is assumed.
  vim.env.TEXMAN_FAKE_MODE = 'fixed'
  local buf, path = open_saved_file('fix10.tex', blamed)
  write_log(path, {
    'This is pdfTeX, Version 3.141592653',
    '! Undefined control sequence.',
    'l.3 Blamed line.',
    'The control sequence at the end of the top line',
  })
  notes = {}
  texman.fix('')
  ok('classic log form is used', wait_for('replaced line 3'), last_note())
  ok('the assumption is stated', string.match(table.concat(
    vim.tbl_map(function(n) return n.message end, notes), ' '), 'assuming this one') ~= nil)
end

do
  -- An unclosed environment names its line in prose, not as l.<n>.
  vim.env.TEXMAN_FAKE_MODE = 'fixed'
  local buf, path = open_saved_file('fix11.tex', blamed)
  write_log(path, {
    'This is pdfTeX, Version 3.141592653',
    '! LaTeX Error: \\begin{equation} on input line 3 ended by \\end{document}.',
    'See the LaTeX manual or LaTeX Companion for explanation.',
  })
  notes = {}
  texman.fix('')
  ok('an "on input line" error is used', wait_for('replaced line 3'), last_note())
  eq('the named line was replaced', vim.api.nvim_buf_get_lines(buf, 2, 3, true),
    { 'Corrected \\emph{line} from the helper.' })
end

do
  -- A runaway argument names no line at all; find it by the echoed source.
  vim.env.TEXMAN_FAKE_MODE = 'fixed'
  local runaway_doc = {
    '\\documentclass{article}',
    '\\begin{document}',
    '\\[',
    '  x = \\frac{-b \\pm \\sqrt{b^2 - 4ac}{2a}',
    '\\]',
    '\\end{document}',
  }
  local buf, path = open_saved_file('fix12.tex', runaway_doc)
  write_log(path, {
    'This is pdfTeX, Version 3.141592653',
    'Runaway argument?',
    '{-b \\pm \\sqrt {b^2 - 4ac}{2a} \\]',
    '! File ended while scanning use of \\frac .',
    '<inserted text>',
    '                \\par',
    '<*> fix12.tex',
    '!  ==> Fatal error occurred, no output PDF file produced!',
  })
  notes = {}
  texman.fix('')
  ok('a runaway argument is located by its text', wait_for('replaced line 4'), last_note())
  eq('the runaway line was replaced', vim.api.nvim_buf_get_lines(buf, 3, 4, true),
    { 'Corrected \\emph{line} from the helper.' })
  eq('other lines untouched', vim.api.nvim_buf_get_lines(buf, 0, 3, true),
    { runaway_doc[1], runaway_doc[2], runaway_doc[3] })
end

do
  -- A fatal error with nothing locatable must be reported, not called clean.
  local buf, path = open_saved_file('fix13.tex', blamed)
  write_log(path, {
    'This is pdfTeX, Version 3.141592653',
    '! Emergency stop.',
    '<*> fix13.tex',
    '!  ==> Fatal error occurred, no output PDF file produced!',
  })
  vim.fn.delete(capture)
  vim.env.TEXMAN_FAKE_CAPTURE = capture
  notes = {}
  texman.fix('')
  vim.wait(200)
  vim.env.TEXMAN_FAKE_CAPTURE = nil
  ok('an unattributable failure is reported honestly',
    string.match(last_note(), 'names no line') ~= nil, last_note())
  ok('the failure message is shown',
    string.match(last_note(), 'Emergency stop') ~= nil, last_note())
  ok('no helper call without a line', vim.fn.filereadable(capture) == 0)
  eq('buffer unchanged for an unattributable failure',
    vim.api.nvim_buf_get_lines(buf, 0, -1, true), blamed)
end

do
  -- A short runaway must not be matched loosely against the buffer.
  local buf, path = open_saved_file('fix14.tex', blamed)
  write_log(path, {
    'Runaway argument?',
    '{x}',
    '! File ended while scanning use of \\foo .',
  })
  notes = {}
  texman.fix('')
  vim.wait(200)
  ok('a too-short runaway is not guessed at',
    string.match(last_note(), 'names no line') ~= nil, last_note())
  eq('buffer unchanged for a short runaway',
    vim.api.nvim_buf_get_lines(buf, 0, -1, true), blamed)
end

do
  local buf = new_tex_buffer(blamed, { filetype = 'markdown', extension = '.md' })
  notes = {}
  texman.fix('')
  vim.wait(150)
  ok('fix refuses a non-TeX buffer', string.match(last_note(), 'not a TeX buffer') ~= nil, last_note())
end

vim.env.TEXMAN_FAKE_MODE = 'ok'

-- ------------------------------------------------------------- :TexAIMap

ok('map command registered', vim.api.nvim_get_commands({})['TexAIMap'] ~= nil)
eq('map command name recorded', texman.map_command_name, 'TexAIMap')
eq('mappings file is the configured one', texman.keymaps_file, keymaps_file)

local function map(args)
  notes = {}
  texman.map(args)
end

do
  vim.fn.delete(capture)
  vim.env.TEXMAN_FAKE_CAPTURE = capture
  map('   ')
  vim.wait(150)
  vim.env.TEXMAN_FAKE_CAPTURE = nil
  ok('an empty mapping request is refused', string.match(last_note(), 'usage') ~= nil, last_note())
  ok('no helper call for an empty mapping request', vim.fn.filereadable(capture) == 0)
end

do
  vim.env.TEXMAN_FAKE_MODE = 'keymap'
  vim.fn.delete(capture)
  vim.env.TEXMAN_FAKE_CAPTURE = capture
  vim.cmd('enew')
  local windows_before = #vim.api.nvim_list_wins()
  map('a shortcut that echoes a greeting')
  ok('a drafted mapping is reported', wait_for('appended'), last_note())
  vim.env.TEXMAN_FAKE_CAPTURE = nil
  local payload = vim.json.decode(table.concat(vim.fn.readfile(capture), '\n'))
  eq('keymap mode is requested', payload.mode, 'keymap')
  eq('the mapping prompt is sent', payload.prompt, 'a shortcut that echoes a greeting')
  eq('an absent mappings file is sent as empty text', payload.keymap_file_text, '')
  ok('mapped keys are sent as a list', type(payload.mapped_keys) == 'table')
  ok('no document lines are sent for a mapping', payload.buffer_lines == nil)
  eq('the mappings file opened in a new window', #vim.api.nvim_list_wins(), windows_before + 1)
  local buf = vim.api.nvim_get_current_buf()
  eq('the split shows the mappings file',
    vim.fn.resolve(vim.api.nvim_buf_get_name(buf)), vim.fn.resolve(keymaps_file))
  local lines = vim.api.nvim_buf_get_lines(buf, 0, -1, true)
  ok('a new mappings file starts with the header', string.match(lines[1], 'TexAIMap') ~= nil, lines[1])
  eq('the request is recorded as a comment', lines[5], '-- a shortcut that echoes a greeting')
  eq('the drafted code follows the comment', lines[6], keymap_snippet)
  eq('the cursor is on the draft', vim.api.nvim_win_get_cursor(0)[1], 5)
  ok('the draft is not written to disk', vim.fn.filereadable(keymaps_file) == 0)
  ok('the buffer is left modified for review', vim.bo[buf].modified)
  ok('the mapping is not active before :w', vim.fn.maparg('<leader>zz', 'n') == '')
  notes = {}
  vim.cmd('silent write')
  ok('writing the file activates it', wait_for('are active'), last_note())
  ok('the mapping is active after :w', vim.fn.maparg('<leader>zz', 'n') ~= '')
  vim.cmd('close')
end

do
  vim.env.TEXMAN_FAKE_MODE = 'keymap'
  vim.fn.delete(capture)
  vim.env.TEXMAN_FAKE_CAPTURE = capture
  map('another shortcut')
  ok('a second draft is appended', wait_for('appended'), last_note())
  vim.env.TEXMAN_FAKE_CAPTURE = nil
  local payload = vim.json.decode(table.concat(vim.fn.readfile(capture), '\n'))
  ok('the existing file is sent as context',
    string.match(payload.keymap_file_text, 'echoes a greeting') ~= nil)
  ok('the active mapping is listed as taken',
    vim.tbl_contains(payload.mapped_keys, 'n \\zz'), vim.inspect(payload.mapped_keys))
  local buf = vim.api.nvim_get_current_buf()
  local on_disk = vim.fn.readfile(keymaps_file)
  local lines = vim.api.nvim_buf_get_lines(buf, 0, -1, true)
  eq('the draft follows the saved contents', #lines, #on_disk + 3)
  eq('a blank line separates drafts', lines[#on_disk + 1], '')
  eq('the second request is recorded', lines[#on_disk + 2], '-- another shortcut')
  vim.cmd('silent undo')
  eq('one undo removes only the draft', vim.api.nvim_buf_get_lines(buf, 0, -1, true), on_disk)
  vim.cmd('close')
end

do
  vim.env.TEXMAN_FAKE_MODE = 'keymap_bad'
  local windows_before = #vim.api.nvim_list_wins()
  local on_disk = vim.fn.readfile(keymaps_file)
  map('a broken shortcut')
  ok('Lua that does not compile is refused', wait_for('does not compile'), last_note())
  eq('no window opens for a refused draft', #vim.api.nvim_list_wins(), windows_before)
  eq('the mappings file is untouched by a refused draft', vim.fn.readfile(keymaps_file), on_disk)
end

do
  vim.env.TEXMAN_FAKE_MODE = 'empty'
  local on_disk = vim.fn.readfile(keymaps_file)
  map('a shortcut with no answer')
  ok('empty mapping output is reported', wait_for('no Lua'), last_note())
  eq('the mappings file is untouched by empty output', vim.fn.readfile(keymaps_file), on_disk)
end

do
  vim.env.TEXMAN_FAKE_MODE = 'keymap_slow'
  map('first mapping')
  notes = {}
  texman.map('second mapping')
  ok('a second mapping request is refused while one runs',
    string.match(last_note(), 'already running') ~= nil, last_note())
  ok('the first mapping request still completes', wait_for('appended'), last_note())
  vim.cmd('silent undo')
  vim.cmd('close')
  vim.env.TEXMAN_FAKE_MODE = 'ok'
end

do
  local broken = tmp .. '/broken-keymaps.lua'
  vim.fn.writefile({ 'vim.keymap.set(' }, broken)
  local saved = texman.keymaps_file
  texman.keymaps_file = broken
  notes = {}
  ok('a broken mappings file is reported, not raised', texman.load_keymaps() == false)
  ok('the report names the problem', string.match(last_note(), 'could not load') ~= nil, last_note())
  texman.keymaps_file = tmp .. '/absent-keymaps.lua'
  ok('a missing mappings file is fine', texman.load_keymaps() == true)
  texman.keymaps_file = saved
end

-- ------------------------------------------------------- whole-file context

do
  vim.env.TEXMAN_FAKE_CAPTURE = capture
  local lines = {}
  for i = 1, 40 do
    lines[i] = 'line ' .. i
  end

  local buf = new_tex_buffer(lines)
  run('20 add an equation')
  ok('a windowed request completes', wait_for('inserted 3 line'), last_note())
  local sent = captured()
  eq('a plain :TexAI is not a whole-file request', sent.full, false)
  eq('the whole buffer still reaches the helper', #sent.buffer_lines, 40)

  notes = {}
  run('20 add an equation', true)
  ok('a bang request completes', wait_for('inserted 3 line'), last_note())
  eq('a bang makes it a whole-file request', captured().full, true)

  -- A fresh buffer, because the insertions above lengthened the last one.
  local counted = new_tex_buffer(lines)
  notes = {}
  run('20 look at the previous 30 lines', true)
  ok('the bang is announced with the real line count',
    string.match(last_note(), 'whole file, 40 lines') ~= nil, last_note())
  ok('the counted request completes', wait_for('inserted 3 line'), last_note())
  vim.api.nvim_buf_delete(counted, { force = true })
  vim.api.nvim_set_current_buf(buf)

  vim.env.TEXMAN_FULL_FILE = '1'
  notes = {}
  run('20 add an equation')
  ok('TEXMAN_FULL_FILE request completes', wait_for('inserted 3 line'), last_note())
  eq('TEXMAN_FULL_FILE makes the bang the default', captured().full, true)
  vim.env.TEXMAN_FULL_FILE = nil

  notes = {}
  run('20 add an equation')
  ok('the default returns when TEXMAN_FULL_FILE is unset',
    wait_for('inserted 3 line'), last_note())
  eq('and the request is windowed again', captured().full, false)

  vim.api.nvim_buf_delete(buf, { force = true })
  vim.env.TEXMAN_FAKE_CAPTURE = nil
end

-- ---------------------------------------------------------- :TexPreamble

ok('setup registers :TexPreamble', texman.preamble_command_name == 'TexPreamble')
ok(':TexPreamble exists', vim.api.nvim_get_commands({})['TexPreamble'] ~= nil)

do
  vim.env.TEXMAN_FAKE_ARGV = argv_capture
  notes = {}
  texman.preamble()
  ok('summarising is reported', string.match(last_note(), 'summarising') ~= nil, last_note())
  ok('the summary completes', wait_for('summarised'), last_note())
  eq('it runs `texman preamble`', vim.fn.readfile(argv_capture)[1], 'preamble')

  notes = {}
  texman.preamble(true)
  ok('a forced summary completes', wait_for('summarised'), last_note())
  eq('a bang forces a refresh', vim.fn.readfile(argv_capture)[1], 'preamble --force')
  vim.env.TEXMAN_FAKE_ARGV = nil
end

do
  vim.env.TEXMAN_FAKE_PREAMBLE = 'fail'
  notes = {}
  texman.preamble()
  ok('a failed summary is reported', wait_for('TEXMAN_OPENAI_MINI_MODEL'), last_note())
  eq('and reported as an error', notes[#notes].level, vim.log.levels.ERROR)
  vim.env.TEXMAN_FAKE_PREAMBLE = nil
end

do
  vim.env.TEXMAN_FAKE_PREAMBLE = 'empty'
  notes = {}
  texman.preamble()
  ok('a silent summary is reported', wait_for('printed nothing'), last_note())
  vim.env.TEXMAN_FAKE_PREAMBLE = nil
end

do
  vim.env.TEXMAN_FAKE_PREAMBLE = 'slow'
  notes = {}
  texman.preamble()
  texman.preamble()
  ok('a second summary is refused while one runs',
    string.match(last_note(), 'already being summarised') ~= nil, last_note())
  ok('the first summary still completes', wait_for('summarised'), last_note())
  vim.env.TEXMAN_FAKE_PREAMBLE = nil
end

-- ------------------------------------------------------------------ summary

vim.fn.delete(tmp, 'rf')
if #failures == 0 then
  print('\nall checks passed')
  os.exit(0)
end
print('\n' .. #failures .. ' check(s) failed: ' .. table.concat(failures, ', '))
os.exit(1)
