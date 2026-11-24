-- Fake witan clients: the outer checkout first, so first-match would pick it.
local root = os.getenv('T')
local got = {}
local function fake(name, folder)
  return {
    name = 'witan',
    workspace_folders = { { uri = vim.uri_from_fname(folder), name = name } },
    request = function(_, method, params)
      table.insert(got, name .. ' ' .. params.command)
      return true, 1
    end,
  }
end
dofile(os.getenv('WITAN_LUA'))
vim.lsp.enable('witan', false)

-- After loading: the config's client() reads vim.lsp.get_clients at call time.
local clients = { fake('outer', root .. '/outer'), fake('inner', root .. '/outer/sub') }
vim.lsp.get_clients = function() return clients end

vim.cmd.edit(root .. '/outer/sub/x.py')
vim.cmd('WitanReady')
vim.cmd.edit(root .. '/outer/y.py')
vim.cmd('WitanReady')
vim.cmd.edit(root .. '/outer/subsidiary/z.py') -- a sibling sharing the prefix
vim.cmd('WitanReady')
io.stdout:write(vim.json.encode(got) .. '\n')
vim.lsp.get_clients = function() return {} end
vim.cmd('qa!')
