# Editor setup: Neovim and Emacs

`witan lsp` is a standard language server on stdio. It serves the council
features (stale-ref diagnostics, the tasks and memories that name the symbol
under the cursor, ready work, launching an agent) and, when witan-code is
installed, the code-graph features (callers and cross-repo impact, interface
hover, cross-repo definition and references). `witan-code lsp` starts the same
server, so run one or the other, not both.

Any editor's LSP client shows the hover and diagnostics with nothing more than
the command to start the server. The configurations below add the two things
LSP leaves to the client: rendering a command's result (a list of tasks, the
callers behind a lens) and running a launch spec in a terminal. They hold no
witan logic; the server decides what to show and what to run
([ADR 0012](adr/0012-editor-integration-architecture.md)).

## Install

```bash
uv tool install ol-agent-kit
```

That installs `witan` and `witan-code` with their `lsp` extras. To install the
servers on their own:

```bash
uv tool install --with 'witan-code[lsp]' 'witan-council[lsp]'
```

The server reads whatever graph `witan` would from each workspace folder: the
local store, or a deployment the folder's target names. Run `witan login`
first for a deployment. The server never prompts: a missing or expired login
shows up as the reason in hover and diagnostics, and a command fails with it.

## Commands the server answers

A client binds these to its command UI. Each takes one object argument whose
`uri` names the workspace folder (or any file in it) the command applies to.

| Command | Arguments | Result |
| --- | --- | --- |
| `witan.listReadyTasks` | `uri` | `{repo, branch, ready, for_branch}` |
| `witan.listProjects` | `uri` | `{repo, projects}` |
| `witan.listMyWork` | `uri` | `{repo, tasks}`, your `in_progress` tasks |
| `witan.listStaleRefs` | `uri` | `{repo, refs}`, every memory ref that no longer resolves, with the file's `uri` when it still exists |
| `witan.listCallers`, `witan.listCrossRepoImpact` | the lens's own arguments | `[{title, repo, path, line, uri}]` |
| `witan.runTask`, `witan.runProject` | `uri`, `slug`, optional `agent` and `model` | a launch spec, or a refusal with `launchable: false`, `reason` and `message` |
| `witan.releaseTask` | `uri`, `slug`, `claimed_at` | `task_release`'s result |

`witan.runTask` claims the task before it answers. A client runs the spec's
`argv` in a terminal at `cwd`, starting from its own environment, removing each
name in `unset_env` and then setting `env`, and refuses a spec whose `version`
it does not know. When the agent does not start, it calls `witan.releaseTask`
with the spec's `claim.claimed_at`, so the task is not held for the 60-minute
lease with nobody working it. The release is fenced on that claim, so it never
undoes a claim the agent has since renewed.

## Neovim

Neovim 0.12 or later, with the built-in client and nothing else. Put this in
your config (e.g. `~/.config/nvim/plugin/witan.lua`):

```lua
-- witan language server for Neovim 0.12+ (built-in vim.lsp).
local LAUNCH_SPEC_VERSION = 1
local LISTS = {
  'witan.listReadyTasks', 'witan.listProjects', 'witan.listMyWork',
  'witan.listStaleRefs', 'witan.listCallers', 'witan.listCrossRepoImpact',
}

-- The folder a command applies to: the current file's, else the cwd's.
local function here()
  local name = vim.api.nvim_buf_get_name(0)
  return vim.uri_from_fname(vim.bo.buftype == '' and name ~= '' and name or vim.fn.getcwd())
end

-- The witan client with a workspace folder holding uri. Neovim starts one
-- client per git root, and a client refuses a folder it does not serve.
local function client(uri)
  local path = vim.uri_to_fname(uri)
  for _, c in ipairs(vim.lsp.get_clients({ name = 'witan' })) do
    for _, folder in ipairs(c.workspace_folders or {}) do
      if vim.fs.relpath(vim.uri_to_fname(folder.uri), path) then return c end
    end
  end
  error('witan: no witan server has a workspace folder holding ' .. path)
end

-- One quickfix item per row: a file location when the row has one.
local function qf_item(row)
  local function get(key) return row[key] ~= vim.NIL and row[key] or nil end
  local text = get('slug') and ('%s (%s) %s'):format(row.slug, get('status') or get('phase') or '', row.title)
    or get('memory') and ('%s %s: %s [%s]'):format(row.memory, row.title, row.symbol_ref, row.status)
    or ('%s  %s %s:%s'):format(row.title, row.repo, row.path, get('line') or '')
  local item = { text = text }
  if get('uri') then
    item.filename = vim.uri_to_fname(row.uri)
    item.lnum = get('line') or 1
  end
  return item
end

local function show_list(command, result)
  local rows = result
  if not vim.islist(result) then
    rows = {}
    for _, key in ipairs({ 'for_branch', 'ready', 'projects', 'tasks', 'refs' }) do
      vim.list_extend(rows, result[key] or {})
    end
  end
  vim.fn.setqflist({}, ' ', { title = command, items = vim.tbl_map(qf_item, rows) })
  vim.cmd.copen()
end

local function exec(c, command, arguments, on_result)
  c:request('workspace/executeCommand', { command = command, arguments = arguments },
    function(err, result)
      if err then
        return vim.notify(command .. ': ' .. err.message, vim.log.levels.ERROR)
      end
      on_result(result)
    end)
end

-- Run a launch spec in a terminal, per ADR 0012's launch contract.
local function run_spec(c, spec, uri)
  if spec.version ~= LAUNCH_SPEC_VERSION then
    return vim.notify(('witan: launch spec version %s is not one this config knows (%d)')
      :format(spec.version, LAUNCH_SPEC_VERSION), vim.log.levels.ERROR)
  end
  if not spec.launchable then
    return vim.notify(('witan: %s not launched: %s'):format(spec.slug, spec.message or spec.reason),
      vim.log.levels.WARN)
  end
  local env = vim.fn.environ()
  for _, name in ipairs(spec.unset_env) do env[name] = nil end
  for name, value in pairs(spec.env) do env[name] = value end
  vim.cmd.tabnew()
  local ok, job = pcall(vim.fn.jobstart, spec.argv, { term = true, cwd = spec.cwd, env = env, clear_env = true })
  if ok and job > 0 then return end
  vim.cmd.tabclose()
  vim.notify(('witan: %s did not start: %s'):format(spec.argv[1], job), vim.log.levels.ERROR)
  if spec.claim ~= vim.NIL then
    -- Don't leave the task held for the lease when no agent is behind it.
    exec(c, 'witan.releaseTask', { { slug = spec.slug, claimed_at = spec.claim.claimed_at, uri = uri } },
      function(r) vim.notify(('witan: released %s: %s'):format(spec.slug, vim.inspect(r))) end)
  end
end

local function launch(command)
  return function(opts)
    local uri = here()
    local c = client(uri)
    exec(c, command, { { slug = opts.args, uri = uri } }, function(spec) run_spec(c, spec, uri) end)
  end
end

local commands = {}
for _, name in ipairs(LISTS) do
  -- Lenses and code actions run their command through these, so the rows
  -- land in quickfix instead of being dropped by the default handler.
  commands[name] = function(cmd, ctx)
    exec(vim.lsp.get_client_by_id(ctx.client_id), cmd.command, cmd.arguments,
      function(result) show_list(cmd.command, result) end)
  end
end

vim.lsp.config('witan', {
  cmd = { 'witan', 'lsp' },
  root_markers = { '.git' },
  commands = commands,
})
vim.lsp.enable('witan')
vim.lsp.codelens.enable(true)

local function list(command)
  return function()
    local uri = here()
    exec(client(uri), command, { { uri = uri } }, function(result) show_list(command, result) end)
  end
end
vim.api.nvim_create_user_command('WitanReady', list('witan.listReadyTasks'), {})
vim.api.nvim_create_user_command('WitanProjects', list('witan.listProjects'), {})
vim.api.nvim_create_user_command('WitanMyWork', list('witan.listMyWork'), {})
vim.api.nvim_create_user_command('WitanStaleRefs', list('witan.listStaleRefs'), {})
vim.api.nvim_create_user_command('WitanRunTask', launch('witan.runTask'), { nargs = 1 })
vim.api.nvim_create_user_command('WitanRunProject', launch('witan.runProject'), { nargs = 1 })
```

CodeLens shows above each function, method and class (`vim.lsp.codelens.run()`
runs the one under the cursor), and the hover is `K`. Stock
`vim.lsp.buf.definition()` and `vim.lsp.buf.references()` give the cross-repo
definition and references. `:WitanReady`, `:WitanProjects`, `:WitanMyWork` and
`:WitanStaleRefs` fill the quickfix list, as does running a lens.
`:WitanRunTask <slug>` and `:WitanRunProject <slug>` open the agent in a
terminal tab.

The server attaches to buffers of every filetype, with the enclosing git
checkout as the workspace folder. To open a second
repo in the same server (so a definition there opens as a file rather than
being named in the hover), `:lua vim.lsp.buf.add_workspace_folder('/path/to/repo')`.

## Emacs

Emacs 30 or later. The helpers below render list results with
`tabulated-list-mode` (`RET` visits a row's file) and run launches with
`make-term`. Load them, then the block for eglot or for lsp-mode.

```elisp
;;; witan language server: list rendering and launches  -*- lexical-binding: t -*-
(require 'cl-lib)
(require 'seq)
(require 'subr-x)
(require 'tabulated-list)

(defconst witan-launch-spec-version 1)
(defconst witan-list-commands
  '("witan.listReadyTasks" "witan.listProjects" "witan.listMyWork"
    "witan.listStaleRefs" "witan.listCallers" "witan.listCrossRepoImpact"))
(defvar witan-execute-function nil
  "Function of (COMMAND ARGUMENTS) returning the server's result. Set below
for eglot or lsp-mode.")

(defun witan--get (object key)
  "KEY of a JSON OBJECT, parsed as a plist (eglot) or a hash table (lsp-mode)."
  (let ((value (if (hash-table-p object)
                   (gethash key object)
                 (plist-get object (intern (concat ":" key))))))
    (unless (eq value :json-false) value)))

(defun witan--here ()
  "The folder a command applies to: the current file's, else `default-directory'."
  (concat "file://" (expand-file-name (or buffer-file-name default-directory))))

(define-derived-mode witan-list-mode tabulated-list-mode "witan"
  "Rows a witan list command returned. RET visits a row's file."
  (setq tabulated-list-format [("Item" 70 t) ("Where" 0 t)])
  (define-key witan-list-mode-map (kbd "RET") #'witan-list-visit))

(defun witan-list-visit ()
  (interactive)
  (when-let* ((row (tabulated-list-get-id))
              (uri (witan--get row "uri")))
    (find-file-other-window (url-unhex-string (string-remove-prefix "file://" uri)))
    (goto-char (point-min))
    (forward-line (1- (or (witan--get row "line") 1)))))

(defun witan--row-text (row)
  (cond ((witan--get row "slug")
         (format "%s (%s) %s" (witan--get row "slug")
                 (or (witan--get row "status") (witan--get row "phase") "")
                 (witan--get row "title")))
        ((witan--get row "memory")
         (format "%s %s: %s [%s]" (witan--get row "memory") (witan--get row "title")
                 (witan--get row "symbol_ref") (witan--get row "status")))
        (t (witan--get row "title"))))

(defun witan-show-list (command result)
  "Render a list COMMAND's RESULT in a `tabulated-list-mode' buffer."
  ;; An array is a vector (eglot) or a list (lsp-mode); an object is a plist,
  ;; which starts with a keyword, or a hash table.
  (let ((rows (if (or (vectorp result) (and (consp result) (not (keywordp (car result)))))
                  (append result nil)
                (cl-loop for key in '("for_branch" "ready" "projects" "tasks" "refs")
                         append (append (witan--get result key) nil)))))
    (with-current-buffer (get-buffer-create (format "*%s*" command))
      (witan-list-mode)
      (setq tabulated-list-entries
            (mapcar (lambda (row)
                      (list row (vector (witan--row-text row)
                                        (if (witan--get row "path")
                                            (format "%s %s:%s" (witan--get row "repo")
                                                    (witan--get row "path")
                                                    (or (witan--get row "line") ""))
                                          ""))))
                    rows))
      (tabulated-list-init-header)
      (tabulated-list-print)
      (pop-to-buffer (current-buffer)))))

(defun witan-list (command)
  (witan-show-list command (funcall witan-execute-function command
                                    (vector (list :uri (witan--here))))))

(defun witan-ready () (interactive) (witan-list "witan.listReadyTasks"))
(defun witan-projects () (interactive) (witan-list "witan.listProjects"))
(defun witan-my-work () (interactive) (witan-list "witan.listMyWork"))
(defun witan-stale-refs () (interactive) (witan-list "witan.listStaleRefs"))

(defun witan-run-spec (spec uri)
  "Run a launch SPEC in a `make-term' buffer, per ADR 0012's launch contract."
  (unless (eql (witan--get spec "version") witan-launch-spec-version)
    (user-error "witan: launch spec version %s is not one this config knows (%s)"
                (witan--get spec "version") witan-launch-spec-version))
  (unless (witan--get spec "launchable")
    (user-error "witan: %s not launched: %s" (witan--get spec "slug")
                (or (witan--get spec "message") (witan--get spec "reason"))))
  (let* ((default-directory (file-name-as-directory (witan--get spec "cwd")))
         (process-environment (copy-sequence process-environment))
         (argv (append (witan--get spec "argv") nil))
         (env (witan--get spec "env"))
         (claim (witan--get spec "claim")))
    (seq-doseq (name (witan--get spec "unset_env")) (setenv name nil))
    (if (hash-table-p env)
        (maphash (lambda (k v) (setenv k v)) env)
      (cl-loop for (k v) on env by #'cddr do (setenv (substring (symbol-name k) 1) v)))
    ;; make-term starts the agent through a shell, so a missing binary would
    ;; not fail here; look it up on the PATH the terminal gets.
    (if (let ((exec-path (parse-colon-path (getenv "PATH"))))
          (executable-find (car argv)))
        (pop-to-buffer (apply #'make-term (format "witan %s" (witan--get spec "slug"))
                              (car argv) nil (cdr argv)))
      (when claim
        ;; Don't leave the task held for the lease with no agent behind it.
        (funcall witan-execute-function "witan.releaseTask"
                 (vector (list :slug (witan--get spec "slug") :uri uri
                               :claimed_at (witan--get claim "claimed_at")))))
      (user-error "witan: %s is not on PATH; %s" (car argv)
                  (if claim (format "released %s" (witan--get spec "slug")) "nothing launched")))))

(defun witan--launch (command slug)
  (let ((uri (witan--here)))
    (witan-run-spec (funcall witan-execute-function command
                             (vector (list :slug slug :uri uri)))
                    uri)))

(defun witan-run-task (slug)
  (interactive "sTask slug: ")
  (witan--launch "witan.runTask" slug))

(defun witan-run-project (slug)
  (interactive "sProject slug: ")
  (witan--launch "witan.runProject" slug))
```

### eglot

eglot is built in. It has no CodeLens, so the server puts each lens's text in
the hover on its line and offers its command as a code action: `M-x
eglot-code-actions` on a function's line runs its callers lens, and the rows
open in a list buffer.

```elisp
;;; witan with eglot (built in)  -*- lexical-binding: t -*-
;;; eglot has no CodeLens: a lens's text is in the hover on its line, and
;;; M-x eglot-code-actions runs its command.
(require 'eglot)

(defclass witan-eglot-server (eglot-lsp-server) ())
(add-to-list 'eglot-server-programs
             '((python-mode python-ts-mode yaml-mode yaml-ts-mode)
               . (witan-eglot-server "witan" "lsp")))

(cl-defmethod eglot-execute :around ((_server witan-eglot-server) action)
  ;; A code action's command lands here; show the rows eglot would drop. Read
  ;; before the default method, which strips :title as it recurses.
  (let* ((list-command (and (plist-get action :title)
                            (car (member (plist-get action :command)
                                         witan-list-commands))))
         (result (cl-call-next-method)))
    (when list-command
      (witan-show-list list-command result))
    result))

(setq witan-execute-function
      (lambda (command arguments)
        (eglot-execute (or (eglot-current-server)
                           (user-error "witan: no eglot server manages this buffer"))
                       (list :command command :arguments arguments))))
```

eglot runs one server per buffer, taken from the first entry in
`eglot-server-programs` that matches the major mode, and `add-to-list` puts
this entry first. So in the modes listed here witan replaces the mode's own
server (pyright for Python, say). List only modes you run no other server in,
or use lsp-mode, where witan runs as an add-on. eglot also starts one server
per project, so a second repo opened in the same Emacs gets its own server,
and a definition in a repo that is not the current project is named in the
hover rather than opened.

### lsp-mode

Registered as an add-on client, so it runs alongside the language server
lsp-mode already starts for the file. Commands go to witan's server only:
lsp-mode would otherwise send each one to every server in the buffer, and a
server that refuses unknown commands (pyright answers "Unsupported command")
would fail the request even when witan answered it. CodeLens shows with `lsp-lens-mode`
(on by default), and clicking a lens opens its rows in a list buffer.

```elisp
;;; witan with lsp-mode  -*- lexical-binding: t -*-
;;; An add-on client, so it runs beside the file's own language server.
(require 'lsp-mode)

(defun witan--lsp-execute (command arguments)
  ;; To witan alone: lsp-request sends a command to every server in the
  ;; buffer, and another server's "unsupported command" error would win.
  (with-lsp-workspace
      (or (seq-find (lambda (workspace)
                      (eq (lsp--client-server-id (lsp--workspace-client workspace))
                          'witan))
                    (lsp-workspaces))
          (user-error "witan: no witan server for this buffer"))
    (lsp-request "workspace/executeCommand"
                 (list :command command :arguments arguments))))

(defun witan--lsp-show (action)
  (let ((command (witan--get action "command")))
    (witan-show-list command (witan--lsp-execute command (witan--get action "arguments")))))

(lsp-register-client
 (make-lsp-client
  :new-connection (lsp-stdio-connection '("witan" "lsp"))
  :activation-fn (lambda (&rest _) t)
  :server-id 'witan
  :add-on? t
  :multi-root t
  :action-handlers (let ((handlers (make-hash-table :test #'equal)))
                     (dolist (command witan-list-commands handlers)
                       (puthash command #'witan--lsp-show handlers)))))

(setq witan-execute-function #'witan--lsp-execute)
```

Opening a file from a second repo with `M-x lsp` adds that repo to the same
server as another workspace folder, so a definition there opens as a file.

In both clients, `M-x witan-ready`, `witan-projects`, `witan-my-work` and
`witan-stale-refs` list work, and `M-x witan-run-task` and `witan-run-project`
open the agent in a terminal buffer.

## What each editor shows

Checked with Neovim 0.12.6, Emacs 31.1's eglot, and lsp-mode 20261109.751,
each driven headlessly with exactly the configuration above against a local
store. lsp-mode ran once with witan alone and once beside ruff and ty. "Fallback" is the per-editor fallback ADR 0012 specifies for a client
that lacks the LSP feature.

| Feature | Neovim | eglot | lsp-mode |
| --- | --- | --- | --- |
| Stale memory ref diagnostic on the file it names | works | works (flymake) | works |
| Tasks and memories naming the symbol under the cursor, in hover | works | works | works |
| Ready work, projects, my work, stale refs lists | works, quickfix | works, `tabulated-list-mode` | works, `tabulated-list-mode` |
| Run a task or project in a terminal | works, `jobstart` terminal tab | works, `make-term` | works, `make-term` |
| Release the claim when the agent does not start | works | works | works |
| Callers lens, its rows as a list | works | fallback: text in hover, code action lists the rows | works |
| Cross-repo impact lens, its rows as a list | works | fallback: text in hover, code action lists the rows | works |
| Interface providers and consumers in hover | works | works | works |
| Definition in another open repo | works, after `add_workspace_folder` | fallback: named in hover, since eglot gives each repo its own server | works, after opening a file there |
| Definition in a repo that is not open | fallback: named in hover | fallback: named in hover | fallback: named in hover |
| References | works | works | works |
| No-index diagnostic | works | works | works |

Nothing is unsupported in any of the three. References cover the definition's
own repo only, in every editor, because the code graph records no
symbol-level use across repos. One rough edge: text from the graph in a hover
is markdown-escaped, and Neovim's hover and eglot without markdown-mode show
the backslashes (`https\:\/\/github\.com`). A server-side fix is tracked
in the editor integration project.
