# GitHub Copilot Custom Agents

Custom agent definitions and instruction files for GitHub Copilot.

## Agent Types

- **Custom instructions** — Markdown files placed at `.github/copilot-instructions.md`
  in a repository to give Copilot persistent context about that repo.
- **Copilot Extensions** — Full extensions built with the Copilot Extensions API.
  Each extension lives in its own subdirectory with a manifest and implementation.

## Structure

```
copilot/
├── instructions/       # Reusable .github/copilot-instructions.md fragments
│   └── <topic>/
│       └── instructions.md
└── extensions/         # Copilot Extension skeletons
    └── <extension-name>/
        └── README.md
```

## Available Agents

| Name | Type | Description |
|------|------|-------------|
| *(none yet)* | | |

## Resources

- [GitHub Copilot Extensions docs](https://docs.github.com/en/copilot/building-copilot-extensions)
- [Copilot custom instructions](https://docs.github.com/en/copilot/customizing-copilot/adding-custom-instructions-for-github-copilot)
