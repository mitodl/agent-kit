### Changed

- Task titles read on their own. The witan-task skill and the `task_create`
  `title` parameter say not to prefix a title with a spec or plan label
  (`A3:`, `Phase 1:`, `Cohort 4:`) and to cite the spec section by name in the
  description instead. Those prefixes reached ready-work lists and session
  context, where a reader from another repo has no way to resolve them.
- The witan-project-tracker skill says how to write a `spec`-phase document so
  it can be cited from elsewhere: headings that say what each section decides,
  and citations that name the section and link it rather than giving "§4" or
  "D3" alone.
- The same guidance reaches every other title surface: `task_update`,
  `workflow_project_create`, `workflow_project_update`, `memory_store`, and
  `witan task create`/`update` and `witan project create` on the CLI. Each
  used to describe a title as a "short label" or "short name".
- The witan-project-tracker and witan-task skills, and the
  `workflow_session_end` `summary` parameter, say to name a task by its title
  and PR rather than its slug or the slug's hex suffix ("blocked on b5c4a8"),
  and to put a decision to the user as the choice itself with a
  recommendation rather than as "decide D3".
- A session summary the Stop hook writes lists each task and memory as its
  title followed by its slug in parentheses, rather than slug first.
  `memory_update`'s `title` parameter gets the same guidance as `memory_store`.
