### Fixed

- A TS/JS import that names the repo root (`import { f } from '.'` or `'./'` in a root-level file, `'..'` from one directory down) resolves to the root `index.ts`/`index.js`. It used to resolve to the path `.`, which matched no file, so those calls had no `Calls` edge and `code_callers` missed them. Existing stores get the edges when the importing file is next indexed, or with one `witan-code reindex`.
