;;; Install lsp-mode from MELPA into LSP_MODE_DIR, not ~/.emacs.d.  -*- lexical-binding: t -*-
;;; emacs --batch -Q -l install_lsp_mode.el, with LSP_MODE_DIR set.
(require 'package)
(setq package-user-dir (or (getenv "LSP_MODE_DIR") (error "LSP_MODE_DIR is not set")))
(add-to-list 'package-archives '("melpa" . "https://melpa.org/packages/") t)
(package-initialize)
(package-refresh-contents)
(package-install 'lsp-mode)
(princ (format "lsp-mode %s in %s\n"
               (package-version-join (package-desc-version (cadr (assq 'lsp-mode package-alist))))
               package-user-dir))
