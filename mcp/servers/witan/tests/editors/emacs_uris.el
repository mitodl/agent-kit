;;; Checks for the URI encoding, list-visit decoding and terminal naming in
;;; the guide's witan-core.el. Prints one JSON object.
(require 'json)
(let* ((root (getenv "T"))
       (paths (list (concat root "/a#b/c.py")
                    (concat root "/100%41/f.py")
                    (concat root "/sp ace/é.py")
                    (concat root "/what?/g.py")))
       (uris (mapcar (lambda (p)
                       (with-temp-buffer
                         (setq buffer-file-name p)
                         (witan--here)))
                     paths))
       ;; witan-list-visit on a row whose uri the server built (Path.as_uri()).
       (visited
        (progn
          (witan-show-list "witan.listStaleRefs"
                           (vector (list :title "t" :uri (getenv "UTF8_URI") :line 1)))
          (with-current-buffer "*witan.listStaleRefs*"
            (goto-char (point-min))
            (witan-list-visit)
            buffer-file-name)))
       ;; Two launches of one slug while the first agent is still alive.
       (spec (list :version 1 :launchable t :slug "wp-x" :cwd root
                   :argv (vector "sleep" "30") :env nil :unset_env [] :claim nil)))
  (witan-run-spec spec "file:///unused")
  (witan-run-spec spec "file:///unused")
  (let ((terms (cl-loop for b in (buffer-list)
                        when (string-prefix-p "*witan wp-x" (buffer-name b))
                        collect (list (buffer-name b)
                                      (process-live-p (get-buffer-process b))))))
    (princ (json-encode `((paths . ,(vconcat paths))
                          (uris . ,(vconcat uris))
                          (visited . ,visited)
                          (terminals . ,(vconcat (mapcar #'vconcat terms))))))
    (terpri)
    (dolist (b (buffer-list))
      (when (get-buffer-process b) (delete-process (get-buffer-process b))))))
