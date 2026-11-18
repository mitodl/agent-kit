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
