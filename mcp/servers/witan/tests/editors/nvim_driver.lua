-- Headless driver for test_editors.py: loads the guide's Neovim config
-- (WITAN_LUA), exercises each feature against the seeded store under ROOT,
-- and records what it saw in RESULT. The assertions are in test_editors.py.
local S = vim.env.ROOT
local out = {}
local function save()
  local f = assert(io.open(vim.env.RESULT, 'w'))
  f:write(vim.json.encode(out))
  f:close()
end
-- Saved after every step, so a run cut off by the timeout still shows how far
-- it got.
local function rec(k, v)
  out[k] = v
  save()
end
local notes = {}
local orig_notify = vim.notify
vim.notify = function(msg, level) table.insert(notes, msg); orig_notify(msg, level) end

local function wait(ms, cond) return vim.wait(ms, cond, 50) end
local function c() return vim.lsp.get_clients({ name = 'witan' })[1] end
local function sync(method, params, bufnr)
  local r = c():request_sync(method, params, 60000, bufnr or 0)
  return r and (r.err or r.result)
end
local function pos_params(line, col)
  return { textDocument = { uri = vim.uri_from_bufnr(0) }, position = { line = line, character = col } }
end
local function qf()
  return vim.tbl_map(function(i) return { text = i.text, file = i.bufnr > 0 and vim.api.nvim_buf_get_name(i.bufnr) or nil, lnum = i.lnum } end, vim.fn.getqflist())
end
local function qf_wait(title)
  wait(60000, function() return vim.fn.getqflist({ title = 1 }).title == title end)
  local items = qf()
  -- Items too, so a command that posts no list cannot pass on the last one.
  vim.fn.setqflist({}, 'r', { title = '', items = {} })
  vim.cmd.cclose()
  return items
end
local function hover_text(h)
  if type(h) ~= 'table' then return h end
  return h.contents and h.contents.value
end

local ok, err = xpcall(function()
-- Inside the xpcall, so a guide block that fails to load is recorded as the
-- driver error rather than leaving Neovim running.
dofile(vim.env.WITAN_LUA)
vim.cmd.edit(S .. '/repos/app/app/service.py')
local buf = vim.api.nvim_get_current_buf()
rec('attached', wait(120000, function() return c() ~= nil and c().initialized end))
rec('server_caps', {
  codeLens = c().server_capabilities.codeLensProvider ~= nil,
  commands = c().server_capabilities.executeCommandProvider.commands,
})
wait(60000, function() return #vim.diagnostic.get(buf) > 0 end)
rec('diagnostics_service', vim.tbl_map(function(d) return d.message end, vim.diagnostic.get(buf)))

-- council hover on `def helper()` (line 9), symbol resolved through witan-code
rec('hover_helper', hover_text(sync('textDocument/hover', pos_params(8, 5))))

-- CodeLens: stock display and run (callers lens on helper)
local lenses = sync('textDocument/codeLens', { textDocument = { uri = vim.uri_from_bufnr(0) } })
rec('codelens_count', type(lenses) == 'table' and #lenses or lenses)
wait(30000, function() return #vim.lsp.codelens.get({ bufnr = buf }) > 0 end)
vim.api.nvim_win_set_cursor(0, { 9, 4 })
vim.lsp.codelens.run()
rec('lens_callers_qf', qf_wait('witan.listCallers'))

-- definition of compute() in helper: lib is not open, so the hover names it
vim.api.nvim_win_set_cursor(0, { 10, 12 })
rec('definition_unopened', sync('textDocument/definition', pos_params(9, 12)))
rec('hover_compute_unopened', hover_text(sync('textDocument/hover', pos_params(9, 12))))
-- open lib as a second workspace folder, then definition jumps there
vim.lsp.buf.add_workspace_folder(S .. '/repos/lib')
wait(2000, function() return false end)
vim.lsp.buf.definition()
wait(30000, function() return vim.api.nvim_buf_get_name(0):match('maths.py') ~= nil end)
rec('definition_jump', { file = vim.api.nvim_buf_get_name(0), cursor = vim.api.nvim_win_get_cursor(0) })
-- references of compute (in lib): stock vim.lsp.buf.references fills quickfix
vim.api.nvim_win_set_cursor(0, { 1, 4 })
vim.lsp.buf.references(nil, { on_list = function(t) vim.fn.setqflist({}, ' ', { title = 'refs', items = t.items }) end })
rec('references_compute', qf_wait('refs'))
vim.cmd.buffer(buf)
vim.api.nvim_win_set_cursor(0, { 9, 4 })
vim.lsp.buf.references(nil, { on_list = function(t) vim.fn.setqflist({}, ' ', { title = 'refs2', items = t.items }) end })
rec('references_helper', qf_wait('refs2'))

-- interface hover and impact lens on settings.py
vim.cmd.edit(S .. '/repos/app/app/settings.py')
wait(10000, function() return #vim.lsp.get_clients({ bufnr = 0, name = 'witan' }) > 0 end)
rec('hover_env_var', hover_text(sync('textDocument/hover', pos_params(4, 25))))
vim.ui.select = function(items, opts, on_choice)
  for _, it in ipairs(items) do
    if opts.format_item(it):match('cross%-repo') then return on_choice(it) end
  end
  on_choice(items[1])
end
vim.api.nvim_win_set_cursor(0, { 4, 4 })
vim.lsp.codelens.run()
rec('lens_impact_qf', qf_wait('witan.listCrossRepoImpact'))

-- list commands render into quickfix
for _, case in ipairs({
  { 'WitanReady', 'witan.listReadyTasks', 'list_ready' },
  { 'WitanProjects', 'witan.listProjects', 'list_projects' },
  { 'WitanMyWork', 'witan.listMyWork', 'list_my_work' },
  { 'WitanStaleRefs', 'witan.listStaleRefs', 'list_stale_refs' },
}) do
  vim.cmd(case[1])
  rec(case[3], qf_wait(case[2]))
end

-- Rewrites the next executeCommand response the witan client gets, for a
-- launch spec the local store would not produce on its own.
local real_request = c().request
local function patch_once(mutate)
  local cl = c()
  cl.request = function(self, method, params, handler, b)
    if method ~= 'workspace/executeCommand' then
      return real_request(self, method, params, handler, b)
    end
    cl.request = real_request
    return real_request(self, method, params, function(err, result, ...)
      if result then mutate(result) end
      return handler(err, result, ...)
    end, b)
  end
end

-- launch a task: terminal job with unset_env removed and env applied. A local
-- store's spec sets no env, so add a name for the config to set.
vim.cmd.edit(S .. '/repos/app/app/service.py')
vim.env.STUB_OUT = S .. '/stub-task.json'
vim.env.EDITOR_MARK = 'kept'
patch_once(function(r) r.env = vim.tbl_extend('force', r.env or {}, { SPEC_MARK = 'set' }) end)
vim.cmd('WitanRunTask ' .. vim.env.TASK)
rec('run_task_stub_ran', wait(60000, function() return vim.uv.fs_stat(S .. '/stub-task.json') ~= nil end))
rec('run_task_terminal', vim.bo.buftype)
vim.cmd.tabclose()
vim.cmd('WitanMyWork')
rec('list_mywork_after_launch', qf_wait('witan.listMyWork'))

-- launch a project
vim.env.STUB_OUT = S .. '/stub-project.json'
vim.cmd('WitanRunProject ' .. vim.env.PROJECT)
rec('run_project_stub_ran', wait(60000, function() return vim.uv.fs_stat(S .. '/stub-project.json') ~= nil end))
vim.cmd.tabclose()

-- an unknown spec version is refused
notes = {}
patch_once(function(r) r.version = 2 end)
vim.env.STUB_OUT = S .. '/stub-v2.json'
local tabs = #vim.api.nvim_list_tabpages()
vim.cmd('WitanRunProject ' .. vim.env.PROJECT)
wait(30000, function() return #notes > 0 end)
rec('version_refused', { notes = notes, tabs_opened = #vim.api.nvim_list_tabpages() - tabs, stub_ran = vim.uv.fs_stat(S .. '/stub-v2.json') ~= nil })

-- a launch whose agent does not start releases the claim it took
notes = {}
patch_once(function(r) if r.argv then r.argv[1] = '/nonexistent/agent' end end)
vim.cmd('WitanRunTask ' .. vim.env.TASK2)
wait(60000, function() for _, n in ipairs(notes) do if n:match('released') then return true end end return false end)
rec('failed_launch_notes', notes)
pcall(vim.cmd.tabclose)
vim.cmd('WitanMyWork')
rec('list_mywork_after_release', qf_wait('witan.listMyWork'))

-- a refused launch: no such task
notes = {}
vim.cmd('WitanRunTask tk-does-not-exist')
wait(30000, function() return #notes > 0 end)
rec('refused_launch_notes', notes)

-- a repo with no index: one Information diagnostic on the first line
vim.cmd.edit(S .. '/repos/stray/m.py')
local sbuf = vim.api.nvim_get_current_buf()
wait(120000, function() return #vim.diagnostic.get(sbuf) > 0 end)
rec('diagnostics_unindexed', vim.tbl_map(function(d) return { d.lnum, d.severity, d.message } end, vim.diagnostic.get(sbuf)))
-- a second root starts a second witan client; commands from here must use it
notes = {}
rec('clients_after_stray', #vim.lsp.get_clients({ name = 'witan' }))
vim.cmd('WitanReady')
wait(30000, function() return vim.fn.getqflist({ title = 1 }).title == 'witan.listReadyTasks' or #notes > 0 end)
rec('stray_ready', { title = vim.fn.getqflist({ title = 1 }).title, notes = notes })
-- from the quickfix window, with the cwd in the second repo: that repo's client
vim.fn.setqflist({}, 'r', { title = '' })
vim.cmd.cd(S .. '/repos/stray')
vim.cmd.copen()
notes = {}
vim.cmd('WitanReady')
wait(30000, function() return vim.fn.getqflist({ title = 1 }).title == 'witan.listReadyTasks' or #notes > 0 end)
rec('quickfix_cwd_ready', { buftype = vim.bo.buftype, title = vim.fn.getqflist({ title = 1 }).title, notes = notes })
end, debug.traceback)
rec('driver_error', not ok and err or vim.NIL)
save()
vim.cmd('qa!')
