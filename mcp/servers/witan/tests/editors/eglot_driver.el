;;; -*- lexical-binding: t -*-
;;; Batch driver for test_editors.py: loads the guide's witan-core.el and eglot
;;; block (WITAN_CORE_EL, WITAN_GLUE_EL), exercises each feature against the
;;; seeded store under ROOT, and records what it saw in RESULT. The
;;; assertions are in test_editors.py.
(require 'json)
(require 'xref)
(defvar S (getenv "ROOT"))
(defvar out nil)
(defvar messages-seen nil)
;; Saved after every step, so a run cut off by the timeout still shows how far
;; it got.
(defun rec (k v)
  (push (cons k v) out)
  (with-temp-file (getenv "RESULT")
    (insert (json-encode (reverse out)))))
(advice-add 'message :before (lambda (fmt &rest args) (when fmt (push (apply #'format fmt args) messages-seen))))

;; CI's Emacs (purcell/nix-emacs-ci) carries a partial backport of the
;; CVE-2024-53920 fix: flymake refuses every backend in a buffer outside
;; `trusted-content', and eglot's backend is not marked safe as the full
;; upstream change marks it. Without this, the diagnostics never reach flymake.
(put 'eglot-flymake-backend 'flymake-always-safe t)

(load (getenv "WITAN_CORE_EL"))
(load (getenv "WITAN_GLUE_EL"))

(defun wait-for (secs pred)
  (let ((end (+ (float-time) secs)))
    (while (and (not (funcall pred)) (< (float-time) end))
      (accept-process-output nil 0.1))
    (funcall pred)))
(defun goto (line col) (goto-char (point-min)) (forward-line (1- line)) (forward-char col))
(defun srv () (eglot-current-server))
(defun hover-at (line col)
  (goto line col)
  (let ((h (eglot--request (srv) :textDocument/hover (eglot--TextDocumentPositionParams))))
    (and h (plist-get (plist-get h :contents) :value))))
(defun list-buffer (command)
  ;; witan-show-list makes the buffer even for no rows, so a missing one means
  ;; the command posted no list: fail rather than record it as an empty one.
  (let ((b (or (get-buffer (format "*%s*" command))
               (error "No *%s* buffer: the command posted no list" command))))
    (prog1 (with-current-buffer b
             (mapcar (lambda (e) (list (aref (cadr e) 0) (aref (cadr e) 1)
                                       (witan--get (car e) "uri")))
                     tabulated-list-entries))
      (kill-buffer b))))
(defun xref-summary (items)
  (mapcar (lambda (x) (let ((loc (xref-item-location x)))
                        (format "%s:%s" (xref-location-group loc) (xref-location-line loc))))
          items))

(condition-case err
    (progn
      (find-file (concat S "/repos/app/app/service.py"))
      (apply #'eglot (eglot--guess-contact))
      (rec "attached" (wait-for 120 (lambda () (and (srv) (eglot--server-capable :hoverProvider)))))
      (rec "class" (symbol-name (eieio-object-class (srv))))
      (rec "codelens_provider" (if (eglot--server-capable :codeLensProvider) t :json-false))
      (rec "code_action_provider" (if (eglot--server-capable :codeActionProvider) t :json-false))
      (wait-for 60 (lambda () (flymake-diagnostics)))
      (rec "diagnostics_service" (mapcar #'flymake-diagnostic-text (flymake-diagnostics)))
      ;; hover on `def helper` carries council's tasks/memories and, as the
      ;; CodeLens fallback, the callers lens text
      (rec "hover_helper" (hover-at 9 5))
      ;; the lens command as a code action
      (goto 9 5)
      (let ((actions (eglot-code-actions (point) nil nil nil)))
        (rec "code_actions_helper" (mapcar (lambda (a) (plist-get a :title)) actions))
        (when actions (eglot-execute (srv) (car actions))))
      (rec "code_action_list" (list-buffer "witan.listCallers"))
      ;; definition of compute(): lib is not a workspace folder
      (goto 10 12)
      (rec "definition_unopened" (xref-summary (xref-backend-definitions 'eglot (xref-backend-identifier-at-point 'eglot))))
      (rec "hover_compute_unopened" (hover-at 10 12))
      ;; lib opened in the same Emacs: eglot starts a second server for it
      (find-file (concat S "/repos/lib/lib/maths.py"))
      (apply #'eglot (eglot--guess-contact))
      (wait-for 60 (lambda () (eglot-current-server)))
      (rec "eglot_lib_servers" (length (hash-table-values eglot--servers-by-project)))
      (find-file (concat S "/repos/app/app/service.py"))
      (goto 10 12)
      (rec "definition_lib_in_other_server" (xref-summary (xref-backend-definitions 'eglot (xref-backend-identifier-at-point 'eglot))))
      (goto 9 5)
      (rec "references_helper" (xref-summary (xref-backend-references 'eglot (xref-backend-identifier-at-point 'eglot))))
      ;; interface hover and the impact lens fallback on settings.py
      (find-file (concat S "/repos/app/app/settings.py"))
      (wait-for 30 (lambda () (eglot-managed-p)))
      (rec "hover_env_var" (hover-at 5 25))
      (rec "hover_base_url" (hover-at 4 5))
      (goto 4 5)
      (let ((actions (eglot-code-actions (point) nil nil nil)))
        (rec "code_actions_base_url" (mapcar (lambda (a) (plist-get a :title)) actions))
        (dolist (a actions)
          (when (string-match-p "cross-repo" (plist-get a :title)) (eglot-execute (srv) a))))
      (rec "impact_list" (list-buffer "witan.listCrossRepoImpact"))
      ;; list commands
      (find-file (concat S "/repos/app/app/service.py"))
      (witan-ready) (rec "list_ready" (list-buffer "witan.listReadyTasks"))
      (witan-projects) (rec "list_projects" (list-buffer "witan.listProjects"))
      (witan-stale-refs) (rec "list_stale_refs" (list-buffer "witan.listStaleRefs"))
      ;; launch a task in a make-term buffer
      (setenv "STUB_OUT" (concat S "/stub-task.json"))
      (setenv "EDITOR_MARK" "kept")
      ;; A local store's spec sets no env, so add a name for the config to set.
      (let* ((real witan-execute-function)
             (witan-execute-function
              (lambda (c a) (let ((r (funcall real c a)))
                              (rec "run_task_argv" (witan--get r "argv"))
                              (plist-put r :env (plist-put (plist-get r :env) :SPEC_MARK "set"))))))
        (witan-run-task (getenv "TASK")))
      (rec "run_task_stub_ran" (wait-for 60 (lambda () (file-exists-p (concat S "/stub-task.json")))))
      (rec "run_task_buffer" (buffer-name))
      (rec "run_task_term_process" (and (get-buffer-process (current-buffer)) t))
      (find-file (concat S "/repos/app/app/service.py"))
      (witan-my-work) (rec "list_mywork_after_launch" (list-buffer "witan.listMyWork"))
      (setenv "STUB_OUT" (concat S "/stub-project.json"))
      (let* ((real witan-execute-function)
             (witan-execute-function
              (lambda (c a) (let ((r (funcall real c a)))
                              (rec "run_project_argv" (witan--get r "argv"))
                              r))))
        (witan-run-project (getenv "PROJECT")))
      (rec "run_project_stub_ran" (wait-for 60 (lambda () (file-exists-p (concat S "/stub-project.json")))))
      (find-file (concat S "/repos/app/app/service.py"))
      ;; an unknown version is refused
      (let ((real witan-execute-function))
        (setq witan-execute-function
              (lambda (c a) (let ((r (funcall real c a))) (plist-put r :version 2))))
        (rec "version_refused" (condition-case e (progn (witan-run-project (getenv "PROJECT")) "ran")
                                 (user-error (error-message-string e))))
        ;; a launch whose agent does not start releases its claim
        (setq witan-execute-function
              (lambda (c a) (let ((r (funcall real c a)))
                              (if (plist-get r :argv) (plist-put r :argv ["/nonexistent/agent"]) r))))
        (rec "failed_launch" (condition-case e (progn (witan-run-task (getenv "TASK2")) "ran")
                               (error (error-message-string e))))
        (find-file (concat S "/repos/app/app/service.py"))
        (setq witan-execute-function real))
      (rec "failed_launch_messages" (seq-filter (lambda (m) (string-match-p "witan" m)) messages-seen))
      (witan-my-work) (rec "list_mywork_after_release" (list-buffer "witan.listMyWork"))
      (rec "refused_launch" (condition-case e (progn (witan-run-task "tk-does-not-exist") "ran")
                              (user-error (error-message-string e))))
      (find-file (concat S "/repos/stray/m.py"))
      (apply #'eglot (eglot--guess-contact))
      (wait-for 120 (lambda () (and (eglot-current-server) (flymake-diagnostics))))
      (rec "diagnostics_unindexed" (mapcar #'flymake-diagnostic-text (flymake-diagnostics))))
  (error (rec "driver_error" (format "%S" err))))
(kill-emacs 0)
