;;; -*- lexical-binding: t -*-
;;; Batch driver for test_editors.py: loads lsp-mode from LSP_MODE_DIR (a
;;; package-user-dir install_lsp_mode.el filled), the guide's witan-core.el and
;;; lsp-mode block (WITAN_CORE_EL, WITAN_GLUE_EL), exercises each feature
;;; against the seeded store under ROOT, and records what it saw in RESULT. The
;;; assertions are in test_editors.py.
(require 'package)
(setq package-user-dir (getenv "LSP_MODE_DIR"))
(package-initialize)
(require 'json)
(require 'xref)
(defvar S (getenv "ROOT"))
(setq lsp-session-file (concat S "/lsp-session")
      lsp-auto-guess-root t
      lsp-enable-file-watchers nil
      lsp-restart 'ignore
      lsp-keep-workspace-alive nil
      lsp-response-timeout 120
      ;; Only witan, so each request is answered by it alone.
      lsp-enabled-clients '(witan)
      lsp-enable-snippet nil)
(defvar out nil)
(defvar messages-seen nil)
;; Saved after every step, so a run cut off by the timeout still shows how far
;; it got.
(defun rec (k v)
  (push (cons k v) out)
  (with-temp-file (getenv "RESULT")
    (insert (json-encode (reverse out)))))
(advice-add 'message :before (lambda (fmt &rest args) (when fmt (push (apply #'format fmt args) messages-seen))))

(load (getenv "WITAN_CORE_EL"))
(load (getenv "WITAN_GLUE_EL"))

(defun wait-for (secs pred)
  (let ((end (+ (float-time) secs)))
    (while (and (not (funcall pred)) (< (float-time) end))
      (accept-process-output nil 0.1))
    (funcall pred)))
(defun goto (line col) (goto-char (point-min)) (forward-line (1- line)) (forward-char col))
(defun hover-at (line col)
  (goto line col)
  (let ((h (lsp-request "textDocument/hover" (lsp--text-document-position-params))))
    (and h (witan--get (witan--get h "contents") "value"))))
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
(defun lens-titles ()
  (mapcar (lambda (ov) (substring-no-properties (or (overlay-get ov 'after-string) (overlay-get ov 'before-string) "")))
          lsp-lens--overlays))
(defun run-lens (line match)
  "Run the lens on LINE whose title matches MATCH, as a click would."
  (goto line 0)
  (let ((lenses (lsp-request "textDocument/codeLens"
                             `(:textDocument (:uri ,(lsp--path-to-uri buffer-file-name))))))
    (seq-doseq (lens lenses)
      (when (= (1- line) (witan--get (witan--get (witan--get lens "range") "start") "line"))
        (let* ((resolved (lsp-request "codeLens/resolve" lens))
               (command (witan--get resolved "command")))
          (push (witan--get command "title") lens-run-log)
          (when (string-match-p match (witan--get command "title"))
            (funcall (lsp-lens--create-interactive-command command))))))))
(defvar lens-run-log nil)

(condition-case err
    (progn
      (find-file (concat S "/repos/app/app/service.py"))
      (lsp)
      (rec "attached" (wait-for 120 (lambda () (and (lsp-workspaces) (seq-every-p (lambda (w) (eq (lsp--workspace-status w) 'initialized)) (lsp-workspaces))))))
      (rec "servers" (mapcar (lambda (w) (symbol-name (lsp--client-server-id (lsp--workspace-client w)))) (lsp-workspaces)))
      (wait-for 60 (lambda () (> (hash-table-count (lsp-diagnostics t)) 0)))
      (rec "diagnostics_service"
           (mapcar (lambda (d) (witan--get d "message"))
                   (gethash buffer-file-name (lsp-diagnostics t))))
      (rec "hover_helper" (hover-at 9 5))
      ;; stock lens display
      (lsp-lens-show)
      (rec "lens_overlays" (lens-titles))
      (run-lens 9 "caller")
      (rec "lens_callers_list" (list-buffer "witan.listCallers"))
      (rec "lens_run_log" lens-run-log)
      (rec "buffers" (mapcar #'buffer-name (buffer-list)))
      (goto 10 12)
      (rec "definition_unopened" (xref-summary (xref-backend-definitions 'xref-lsp (xref-backend-identifier-at-point 'xref-lsp))))
      (rec "hover_compute_unopened" (hover-at 10 12))
      ;; a file from lib joins the same (multi-root) server as a second folder
      (find-file (concat S "/repos/lib/lib/maths.py"))
      (lsp)
      (wait-for 30 (lambda () (lsp-workspaces)))
      (rec "servers_after_lib" (mapcar (lambda (w) (format "%s %S" (lsp--workspace-print w) (mapcar #'lsp--workspace-root (list w)))) (lsp-workspaces)))
      (find-file (concat S "/repos/app/app/service.py"))
      (goto 10 12)
      (rec "definition_opened" (xref-summary (xref-backend-definitions 'xref-lsp (xref-backend-identifier-at-point 'xref-lsp))))
      (goto 9 5)
      (rec "references_helper" (xref-summary (xref-backend-references 'xref-lsp (xref-backend-identifier-at-point 'xref-lsp))))
      (find-file (concat S "/repos/app/app/settings.py"))
      (lsp)
      (wait-for 30 (lambda () (lsp-workspaces)))
      (rec "hover_env_var" (hover-at 5 25))
      (run-lens 4 "cross-repo")
      (rec "impact_list" (list-buffer "witan.listCrossRepoImpact"))
      (find-file (concat S "/repos/app/app/service.py"))
      (witan-ready) (rec "list_ready" (list-buffer "witan.listReadyTasks"))
      (witan-projects) (rec "list_projects" (list-buffer "witan.listProjects"))
      (witan-stale-refs) (rec "list_stale_refs" (list-buffer "witan.listStaleRefs"))
      (setenv "STUB_OUT" (concat S "/stub-task.json"))
      (setenv "EDITOR_MARK" "kept")
      ;; A local store's spec sets no env, so add a name for the config to set.
      (let* ((real witan-execute-function)
             (witan-execute-function
              (lambda (c a) (let ((r (funcall real c a)))
                              (rec "run_task_argv" (witan--get r "argv"))
                              (unless (hash-table-p (gethash "env" r))
                                (puthash "env" (make-hash-table :test #'equal) r))
                              (puthash "SPEC_MARK" "set" (gethash "env" r))
                              r))))
        (witan-run-task (getenv "TASK")))
      (rec "run_task_stub_ran" (wait-for 60 (lambda () (file-exists-p (concat S "/stub-task.json")))))
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
      (let ((real witan-execute-function))
        (setq witan-execute-function
              (lambda (c a) (let ((r (funcall real c a))) (puthash "version" 2 r) r)))
        (rec "version_refused" (condition-case e (progn (witan-run-project (getenv "PROJECT")) "ran")
                                 (user-error (error-message-string e))))
        (setq witan-execute-function
              (lambda (c a) (let ((r (funcall real c a)))
                              (when (gethash "argv" r) (puthash "argv" ["/nonexistent/agent"] r)) r)))
        (rec "failed_launch" (condition-case e (progn (witan-run-task (getenv "TASK2")) "ran")
                               (error (error-message-string e))))
        (find-file (concat S "/repos/app/app/service.py"))
        (setq witan-execute-function real))
      (rec "failed_launch_messages" (seq-filter (lambda (m) (string-match-p "witan" m)) messages-seen))
      (witan-my-work) (rec "list_mywork_after_release" (list-buffer "witan.listMyWork"))
      (rec "refused_launch" (condition-case e (progn (witan-run-task "tk-does-not-exist") "ran")
                              (user-error (error-message-string e))))
      (find-file (concat S "/repos/stray/m.py"))
      (lsp)
      (wait-for 120 (lambda () (gethash buffer-file-name (lsp-diagnostics t))))
      (rec "diagnostics_unindexed" (mapcar (lambda (d) (witan--get d "message")) (gethash buffer-file-name (lsp-diagnostics t)))))
  (error (rec "driver_error" (format "%S" err))))
(kill-emacs 0)
