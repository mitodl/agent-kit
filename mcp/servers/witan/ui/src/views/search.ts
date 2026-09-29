import { html, nothing, type TemplateResult } from "lit-html";
import { emptyBox } from "../chrome.js";
import { absolute, ago, repoLabel } from "../format.js";
import { type Route, routeHref } from "../route.js";
import type {
	TaskCore,
	TaskRow,
	TaskSearchRow,
	WorkflowProjectSummary,
} from "../types.js";

/**
 * The Search view: projects and tasks matching the header's search text.
 *
 * The two halves are found differently. Projects are matched in the browser
 * against the list the shell already holds, which is small by construction
 * and is every active project. Tasks are not held anywhere whole, so they come
 * from a read (see `readSearch` in `app.ts`).
 */

/**
 * `task_search`'s cap (`_SEARCH_LIMIT` in `server.py`). The tool takes no
 * limit, so a result this long may have left matches out.
 */
export const SEARCH_LIMIT = 20;

export interface SearchResults {
	/** Ranked by BM25 from `task_search`, or a project's tasks in list order. */
	tasks: (TaskRow | TaskSearchRow)[];
	/** Whether the tasks came from `task_search` and hit its cap. */
	capped: boolean;
}

/**
 * Whether every whitespace-separated word of `query` appears, case-folded, in
 * one of `fields`.
 *
 * Every word rather than the phrase, so "ui search" finds "Search in the witan
 * UI"; a substring rather than a token match, so a slug fragment such as
 * `3b8d0f` finds its task.
 */
export function matchesText(
	fields: (string | null | undefined)[],
	query: string,
): boolean {
	const haystack = fields.filter(Boolean).join("\n").toLowerCase();
	return query
		.toLowerCase()
		.split(/\s+/)
		.filter(Boolean)
		.every((word) => haystack.includes(word));
}

export function projectMatches(
	project: WorkflowProjectSummary,
	query: string,
): boolean {
	return matchesText(
		[project.title, project.slug, project.phase, ...(project.tags ?? [])],
		query,
	);
}

/**
 * The fields a task is matched on inside one project.
 *
 * `task_list` rows carry no description, so this is narrower than what
 * `task_search` matches on outside a project.
 */
export function taskMatches(task: TaskCore, query: string): boolean {
	return matchesText(
		[task.title, task.slug, task.assignee, ...(task.tags ?? [])],
		query,
	);
}

export function searchView(
	results: SearchResults,
	projects: WorkflowProjectSummary[],
	route: Route,
	now = Date.now(),
): TemplateResult {
	const matched = projects.filter(
		(project) =>
			(!route.project || project.slug === route.project) &&
			projectMatches(project, route.find),
	);
	// Inside a project the read has every status, so the Closed toggle filters
	// here without a re-read. Outside one the read already left them out.
	const tasks = results.tasks.filter(
		(task) => route.closed || task.status !== "closed",
	);
	return html`
    <div class="search-view">
      <section>
        <h3>Projects <span class="count">${matched.length}</span></h3>
        ${
					matched.length === 0
						? emptyBox("No active projects matched.")
						: projectRows(matched, route)
				}
      </section>
      <section>
        <h3>Tasks <span class="count">${tasks.length}</span></h3>
        ${
					results.capped
						? html`<p class="note">
              Task search returns its ${SEARCH_LIMIT} best matches, so there may
              be more. Choose a project to search all of its tasks.
            </p>`
						: nothing
				}
        ${
					tasks.length === 0
						? emptyBox(
								route.closed
									? "No tasks matched."
									: "No open tasks matched. Tick Closed to include finished work.",
							)
						: taskRows(
								tasks,
								new Map(
									projects.map((project) => [project.slug, project.title]),
								),
								route,
								now,
							)
				}
      </section>
    </div>
  `;
}

function projectRows(
	projects: WorkflowProjectSummary[],
	route: Route,
): TemplateResult {
	return html`
    <table class="rows">
      <thead>
        <tr>
          <th>Project</th>
          <th>Phase</th>
          <th>Repos</th>
          <th>Updated</th>
        </tr>
      </thead>
      <tbody>
        ${projects.map(
					(project) => html`
            <tr>
              <td>
                <a
                  href=${routeHref(route, {
										view: "projects",
										project: project.slug,
										find: "",
									})}
                  >${project.title}</a
                >
              </td>
              <td>${project.phase}</td>
              <td>${(project.repos ?? []).map(repoLabel).join(", ") || "—"}</td>
              <td title=${absolute(project.updated_at)}>
                ${ago(project.updated_at)}
              </td>
            </tr>
          `,
				)}
      </tbody>
    </table>
  `;
}

/**
 * With a Project column, which a project's own task table has no need for.
 * A project outside the list (closed, or in another repo) shows its slug.
 */
function taskRows(
	tasks: TaskCore[],
	titles: Map<string, string>,
	route: Route,
	now: number,
): TemplateResult {
	return html`
    <table class="rows">
      <thead>
        <tr>
          <th>Task</th>
          <th>Status</th>
          <th>Pri</th>
          <th>Project</th>
          <th>Repo</th>
          <th>Updated</th>
        </tr>
      </thead>
      <tbody>
        ${tasks.map(
					(task) => html`
            <tr class=${task.status === "closed" ? "closed" : ""}>
              <td>
                <a href=${routeHref(route, { slug: task.slug })}>${task.title}</a>
              </td>
              <td>${task.status}</td>
              <td>${task.priority}</td>
              <td>
                ${
									task.project_slug
										? html`<a
                        href=${routeHref(route, {
													view: "projects",
													project: task.project_slug,
													find: "",
												})}
                        >${
													titles.get(task.project_slug) ??
													html`<code>${task.project_slug}</code>`
												}</a
                      >`
										: "—"
								}
              </td>
              <td>${task.repo ? repoLabel(task.repo) : "—"}</td>
              <td title=${absolute(task.updated_at)}>${ago(task.updated_at, now)}</td>
            </tr>
          `,
				)}
      </tbody>
    </table>
  `;
}
